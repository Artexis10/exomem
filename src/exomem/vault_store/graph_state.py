"""The graph state that a store-mode vault reports (OpenSpec make-vault-store-canonical, task 3.2).

File mode keeps its graph state in an epoch of files (`graph_sync`): a checkpoint, a floor and the
sidecar's acknowledgement. Migration leaves that epoch where it was, and nothing advances it afterwards,
because each store commit derives its pages' graph rows in its own transaction. A status or a gate that
read the epoch would report the frozen file-mode generation, so each reader that reports or gates on
the graph's state asks here first. A file-mode vault keeps file mode's own answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..collection_store.connection import CollectionStoreError


@dataclass(frozen=True)
class StoreGraph:
    """What a store-mode graph reader serves: the revision it reads at, or why it serves nothing."""

    current: bool
    revision: int | None
    reason: str | None = None


def read(vault_root: Path) -> StoreGraph | None:
    """The store's graph state, or None for a file-mode vault, whose epoch answers instead.

    The graph is current when it is enabled and the store opens: this is the readiness that
    `EpistemicGraphIndex._open_read_snapshot` applies before it lends the store's snapshot.
    """
    from . import mode
    from . import read as store_read

    if not mode.store_mode_for_reads(vault_root):
        return None
    from ..epistemic_graph import graph_enabled

    if not graph_enabled():
        return StoreGraph(False, None, "graph_disabled")
    try:
        return StoreGraph(True, store_read.current_revision(vault_root))
    except CollectionStoreError as error:
        return StoreGraph(False, None, error.code)


def is_current(vault_root: Path) -> bool:
    """Whether the vault's graph projection is current, in file mode by its epoch and in store mode by the store."""
    from .. import graph_sync

    store = read(vault_root)
    if store is not None:
        return store.current
    return graph_sync.status(vault_root)["state"] == "current"
