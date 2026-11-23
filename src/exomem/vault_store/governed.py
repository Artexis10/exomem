"""Governance around a store-mode lifecycle commit (OpenSpec make-vault-store-canonical D2, D7).

A governed page's trash and recovery keep file mode's protocol, and the store commit takes the
place of the held rename. Before the commit, ``lifecycle`` writes the receipt intent and the
tombstone (or the staged recovery marker) under the governance folder, which the store does not
own. After it, the writer drops the vectors of the trashed paths, and ``lifecycle`` settles the
tombstone and the receipt from what the store and the vector and CLIP sidecars hold. An item that
no policy governs takes none of this.

The crash windows are file mode's, with the commit where the rename was:

- A kill before the commit leaves a pending tombstone or a staged marker whose items are still at
  their source. Reconcile aborts the receipt and removes the marker.
- A kill after the commit leaves one whose items are placed. Reconcile commits the receipt once no
  derived row of a trashed path is left, or once a restored page's derivatives are exact.
- A kill between the commit and the vector purge leaves vector rows of a trashed path. The vector
  stamp rule withholds them from readers, and the drain purges them.
- A kill after some binaries of a folder were renamed (``binaries`` names that window) splits the
  folder between its source and the trash. Neither side matches the manifest, so reconcile leaves
  the tombstone pending, and it withholds both sides until an operator resolves the split.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def begin_deletion(
    root: Path, *, source_rel: str, trash_rel: str, error_class: Callable[..., Exception]
) -> Any:
    """The governed deletion of ``source_rel``, or None when nothing that it trashes is governed.

    Run it after every refusal of the writer and before its commit: it writes the receipt intent
    and the pending tombstone. A refusal raises ``error_class`` with the lifecycle's code.
    """
    from ..governance import lifecycle

    try:
        return lifecycle.begin_store_deletion(root, source_rel=source_rel, trash_rel=trash_rel)
    except lifecycle.LifecycleError as error:
        raise error_class(code=error.code, reason=error.reason) from error


def begin_recovery(
    root: Path, *, trash_rel: str, restore_rel: str, error_class: Callable[..., Exception]
) -> Any:
    """The governed recovery of ``trash_rel``, or None when nothing that it restores is governed."""
    from ..governance import lifecycle

    try:
        return lifecycle.begin_store_recovery(root, trash_rel=trash_rel, source_rel=restore_rel)
    except lifecycle.LifecycleError as error:
        raise error_class(code=error.code, reason=error.reason) from error


@contextmanager
def committing(operation: Any) -> Iterator[None]:
    """Around the store commit: a refusal aborts the governed transition, and the commit marks it
    placed.

    The abort settles the receipt only while every item is still at its source, so a commit that
    refused, which moves nothing, aborts, and a write that raised after its commit and its renames
    stays pending for reconcile.
    """
    from ..governance import lifecycle

    try:
        yield
    except BaseException:
        if operation is not None:
            if operation.operation == "recovery":
                lifecycle.abort_recovery(operation)
            else:
                lifecycle.abort_deletion(operation)
        raise
    if operation is not None:
        lifecycle.placed_in_store(operation)


def settle(root: Path, operation: Any, kind: str, paths: list[str], warnings: list[str]) -> dict:
    """The writer's index report of ``paths`` once the governed transition is settled, after its
    commit and its own cleanup of what lives outside the store.

    A governed transition first derives the rows that its commit queued, under the boundary that it
    still holds: the settling proves them exact, and the request would derive them only after the
    release (``derivation``). A deletion then drops the vectors of every trashed path, which the drain
    would drop later, and the report says how that went. The settling commits the receipt only when
    no derived row of a trashed path is left, or when every restored page's derivatives are exact;
    otherwise the content stays tombstoned and the writer warns, as file mode does. A write that no
    policy governs reports its rows as its commit left them, and its request derives them.
    """
    from .. import index_sync
    from ..governance import lifecycle
    from . import batch

    if operation is not None:
        _derive_queued(root)
    report = batch.row_report(kind, paths, root=root)
    if operation is None:
        return report.as_dict()
    if operation.operation == "recovery":
        index = report.as_dict()
        if not lifecycle.finish_recovery(operation, index_report=index):
            warnings.append(lifecycle.RECOVERY_NOT_EXACT)
        return index
    index = index_sync.with_component(report, _purge_vectors(root, operation)).as_dict()
    if not lifecycle.finish_deletion(operation, index_report=index):
        warnings.append(lifecycle.TRASH_NOT_EXACT)
    return index


def _derive_queued(root: Path) -> None:
    """Derive the due intents of ``root``, this transition's own among them, within the request's
    own derivation bound. One that is left reads as deferred, and the settling leaves the transition
    to reconcile."""
    from ..collection_store.preview import bound_writer
    from . import derivation

    writer = bound_writer(Path(root).resolve())
    if writer is None:
        return
    try:
        derivation.drain(
            Path(writer.root), writer, deadline=time.monotonic() + derivation.OWN_DERIVATION_SECONDS
        )
    except Exception:  # noqa: BLE001 - the report then names the rows deferred, and reconcile settles it
        log.warning(
            "the rows of a governed transition were not derived before its settling", exc_info=True
        )


def _purge_vectors(root: Path, operation: Any) -> Any:
    """Drop the vector rows of a governed deletion's paths: the drain's own purge, run now so that
    the settling does not wait for the drain. At most ``receipts.MAX_OUTCOMES`` items and their
    frames, in one transaction."""
    from .. import embeddings, index_paths, index_sync

    rels = sorted({*(item.source_path for item in operation.manifest), *operation.residue_paths})
    if not index_paths.sidecar_path(root).exists():
        return index_sync.IndexComponentOutcome("embeddings", "not_required", "no_vector_sidecar")
    try:
        embeddings.get_embedding_index(root).purge_paths_if_present(rels)
    except Exception:  # noqa: BLE001 - the commit is durable; the settling sees the rows left
        log.warning("the vector purge of a governed deletion failed", exc_info=True)
        return index_sync.IndexComponentOutcome("embeddings", "failed", "governed_purge")
    return index_sync.IndexComponentOutcome("embeddings", "completed", "governed_purge")
