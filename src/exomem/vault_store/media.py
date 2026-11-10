"""The store-mode media writer (OpenSpec make-vault-store-canonical, tasks 6.4 and 6.5).

A media binary stays a file. Its derived rows are the sidecar page that ``media_processing`` writes
beside it, with that page's lexical and graph rows. A store-mode vault commits a page only on the
store thread, so the importer hands each reconciliation to the store thread's runner and never calls
``media_processing`` from the watcher's thread, where the store refuses it.

``derive`` reconciles the media binaries that an outside change just recorded. A reconciliation that
fails is named in the report, and ``recover`` is the sweep's bounded pass that finds what a failure
or a kill left without its rows, as the file-mode watcher's periodic pass does.

The media store evicts an original's bytes and restores them under its own mutation guard. Those two
changes of the file are not outside edits, so ``managed`` asks ``media_store.managed_event``, the
answer that the file-mode watcher already acts on, before the importer reads a missing or new file
as a delete or a create.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from .. import media_processing, media_store
from ..collection_store import preview
from . import mode, ownership, projector
from .projector import Runner

if TYPE_CHECKING:
    from ..collection_store.writer import CollectionWriter

log = logging.getLogger(__name__)

_Result = TypeVar("_Result")

#: What ``managed`` answers for a file that the media store is moving now: look again later.
DEFER = "defer"
#: What ``managed`` answers for a file that the media store's own eviction or restore changed.
SUPPRESS = "suppress"


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
    """Whether a store-mode vault's media sidecar commit starts on a thread that cannot commit."""
    return mode.store_mode_active(root) and preview.bound_writer(Path(root).resolve()) is None


def on_store_thread(root: Path, work: Callable[[], _Result]) -> _Result:
    """Run ``work`` on the store thread and return what it returns, as the importer's jobs run.

    The media worker's result arrives on its own thread, after the importer settled the binary. Its
    sidecar commit takes this door instead of committing from the worker, which the store refuses.
    """
    outcome: list[_Result] = []

    def job(_writer: CollectionWriter) -> None:
        outcome.append(work())

    projector.store_thread_runner(root)(job)
    return outcome[0]


def derive(root: Path, run: Runner, rels: Iterable[str]) -> tuple[list[str], dict[str, str]]:
    """Reconcile each governed media binary on the store thread: the paths done, and the failures.

    Each path is its own store-thread job, so a governed write runs between two of them and a large
    file's hash holds the store thread for that one path only. A failure never stops the next path:
    it is returned with its code and the sweep's ``recover`` pass tries the path again.
    """
    derived: list[str] = []
    failed: dict[str, str] = {}
    for rel in dict.fromkeys(rels):
        if not governed(rel):
            continue
        outcome: list[object] = []

        def work(
            _writer: CollectionWriter, rel: str = rel, outcome: list[object] = outcome
        ) -> None:
            outcome.append(media_processing.reconcile_media(root, rel, explicit=False))

        try:
            run(work)
        except Exception as error:  # noqa: BLE001 - one unreadable binary must not stop the others
            failed[rel] = getattr(error, "code", type(error).__name__)
            log.warning("media derivation failed for %s", rel, exc_info=True)
        else:
            if outcome and outcome[0] is not None:
                derived.append(rel)
    return derived, failed


def recover(root: Path, run: Runner) -> int:
    """One bounded, rotating pass that reconciles the media binaries still without their rows.

    It is ``media_processing.reconcile_all_media``, with the limit of the file-mode watcher's pass
    and each reconciliation on the store thread; the number of binaries it reconciled is returned.
    """

    def on_store_thread(binary: Path) -> None:
        def work(_writer: CollectionWriter) -> None:
            media_processing.reconcile_media(root, binary, explicit=False)

        run(work)

    return media_processing.reconcile_all_media(
        root, limit=media_processing.DEFAULT_RECONCILE_LIMIT, reconcile_one=on_store_thread
    )


def managed(root: Path, rel: str) -> str | None:
    """``SUPPRESS`` or ``DEFER`` when the media store itself changed or is changing ``rel``'s file.

    A file whose state cannot be read is deferred: reading an eviction as an outside delete would
    drop the binary's record, and a deferred path is looked at again by the next event or sweep.
    """
    if ownership.classify(rel) is not ownership.Ownership.BINARY:
        return None
    try:
        return media_store.managed_event(root, root / rel)
    except Exception:  # noqa: BLE001 - an unreadable media ledger is no proof that this is an outside change
        log.warning("media store state unreadable for %s", rel, exc_info=True)
        return DEFER
