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
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
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
    stays pending for reconcile. A governed transition's request derives its rows before it settles
    and acknowledges (``derivation.own_rows``), so its reply reports the rows that settled it.
    """
    from ..governance import lifecycle
    from . import derivation

    try:
        with derivation.own_rows() if operation is not None else nullcontext():
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
    """The writer's index report of ``paths`` after the governed transition's commit and its own
    cleanup of what lives outside the store.

    A deletion first drops the vectors of every trashed path, which the drain would drop later, and
    the report says how that went. The settling commits the receipt only when no derived row of a
    trashed path is left, or when every restored page's derivatives are exact; otherwise the content
    stays tombstoned and the writer warns, as file mode does.

    The commit queued the transition's rows, and its request derives them after it releases the
    boundary (``derivation``), ahead of any older backlog and within one slice. So in a request the
    settling runs after that derivation, in a hold of its own (``_settle_after_release``), and the
    acknowledgement carries its report and warning. A transition whose own rows take more than a
    slice settles not exact, as does one whose derivation fails, and reconcile settles it. A caller
    with no request settles here, from the rows as they stand.
    """
    from .. import index_sync, writer_lease
    from . import batch

    report = batch.row_report(kind, paths, root=root)
    if operation is None:
        return report.as_dict()
    purged = None if operation.operation == "recovery" else _purge_vectors(root, operation)

    def with_purge(rows: Any) -> Any:
        return rows if purged is None else index_sync.with_component(rows, purged)

    if writer_lease.defer_until_terminal_persisted(
        lambda: _settle_after_release(root, operation, kind, paths, with_purge)
    ):
        return with_purge(report).as_dict()
    return _finish(operation, with_purge(report), warnings)


def _settle_after_release(
    root: Path, operation: Any, kind: str, paths: list[str], with_purge: Callable[[Any], Any]
) -> list[object]:
    """Settle the transition once its request has derived its rows, in a boundary hold that reads
    their state and records the receipt; the derivation itself never runs under the boundary.

    This runs on the store thread, after the request's own derivation (``derivation.own_rows``): a
    second derivation here would add nothing, and a wait for the thread to derive would stall the
    thread that it waits for. Its report, and the not-exact warning when it leaves the content
    tombstoned, go to the acknowledgement. A settling that fails leaves the content tombstoned for
    reconcile, and the acknowledgement says that too.
    """
    from .. import writer_lease
    from . import batch

    try:
        with writer_lease.active_manager().mutation_guard(root, operation="governed_settle"):
            return _settled(operation, with_purge(batch.row_report(kind, paths, root=root)))
    except Exception:  # noqa: BLE001 - the commit is durable; reconcile settles the receipt
        from ..mutation_terminal import LeafWarning

        log.warning("a governed transition could not settle after its commit", exc_info=True)
        return [LeafWarning(_not_exact(operation))]


def _settled(operation: Any, report: Any) -> list[object]:
    """The settled transition as its acknowledgement takes it: its report, and its warning if any."""
    from ..mutation_terminal import LeafWarning

    warnings: list[str] = []
    _finish(operation, report, warnings)
    return [report, *(LeafWarning(warning) for warning in warnings)]


def _finish(operation: Any, report: Any, warnings: list[str]) -> dict:
    """Commit the transition's receipt when ``report`` proves its derived state exact, else warn."""
    from ..governance import lifecycle

    index = report.as_dict()
    finish = (
        lifecycle.finish_recovery
        if operation.operation == "recovery"
        else lifecycle.finish_deletion
    )
    if not finish(operation, index_report=index):
        warnings.append(_not_exact(operation))
    return index


def _not_exact(operation: Any) -> str:
    """The warning of a transition whose content stays tombstoned until reconcile."""
    from ..governance import lifecycle

    return (
        lifecycle.RECOVERY_NOT_EXACT
        if operation.operation == "recovery"
        else lifecycle.TRASH_NOT_EXACT
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
