"""The graph state that a store-mode vault reports (OpenSpec make-vault-store-canonical, task 3.2).

File mode keeps its graph state in an epoch of files (`graph_sync`): a checkpoint, a floor and the
sidecar's acknowledgement. Migration leaves that epoch where it was, and nothing advances it afterwards,
because each store commit derives its pages' graph rows in its own transaction. A status or a gate that
read the epoch would report the frozen file-mode generation, so each reader that reports or gates on
the graph's state asks here first. A file-mode vault keeps file mode's own answer.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from ..collection_store.connection import CollectionStoreError

#: ``StoreGraph.reason`` of a graph that the build has switched off, and of a store whose read failed with no
#: store code of its own.
GRAPH_DISABLED = "graph_disabled"
STORE_UNREADABLE = "store_unreadable"
#: ``StoreGraph.state`` of a graph that the build has switched off. A failed read is ``unknown`` instead.
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class StoreGraph:
    """What a store-mode graph reader serves: the revision it reads at, or why it serves nothing.

    ``current`` says the graph serves rows: it is enabled and the store opens. It does not say how fresh
    the rows are: ``registry`` is `rederive`'s state of the snapshot (``current``, ``rebuilding`` or
    ``unknown``), which stays behind while a schema or access-policy pass re-derives the pages. It is
    ``unknown`` for any read that failed, with its code in ``reason``, and None only for a graph that
    the build has switched off.
    """

    current: bool
    revision: int | None
    reason: str | None = None
    registry: str | None = None

    @property
    def state(self) -> str:
        """The one word that doctor and the writer-lease status report for the graph."""
        return UNAVAILABLE if self.registry is None else self.registry


def read(vault_root: Path) -> StoreGraph | None:
    """The store's graph state, or None for a file-mode vault, whose epoch answers instead.

    The graph is current when it is enabled and the store opens: this is the readiness that
    `EpistemicGraphIndex._open_read_snapshot` applies before it lends the store's snapshot.
    A read that fails reports an unknown state with its code, never a crash and never a healthy answer.
    """
    from .. import state_migration
    from . import mode
    from . import read as store_read

    try:
        if not mode.store_mode_for_reads(vault_root):
            return None
        from ..epistemic_graph import graph_enabled

        if not graph_enabled():
            return StoreGraph(False, None, GRAPH_DISABLED)
        readiness = store_read.readiness(vault_root)
        return StoreGraph(True, readiness.revision, registry=readiness.registry)
    except CollectionStoreError as error:
        return StoreGraph(False, None, error.code, mode.UNKNOWN)
    except state_migration.StateMigrationManifestError as error:
        return StoreGraph(False, None, error.code, mode.UNKNOWN)
    except sqlite3.Error:
        return StoreGraph(False, None, STORE_UNREADABLE, mode.UNKNOWN)


def is_current(vault_root: Path) -> bool:
    """Whether the vault's graph projection is current, in file mode by its epoch and in store mode by the store."""
    from .. import graph_sync

    store = read(vault_root)
    if store is not None:
        return store.current
    return graph_sync.status(vault_root)["state"] == "current"
