"""What a vault in store mode refuses while nothing can serve its pages (OpenSpec make-vault-store-canonical, task 1.6).

A store-mode vault keeps its pages in the collection store, so each path that would serve stale
Markdown, copy pages to another host, or swap the live store for a copy without pages must refuse.
A file-mode vault takes none of these branches: every answer starts from ``mode.store_mode_active``,
which reads the manifest first and never opens the store of a file-mode vault.
"""

from __future__ import annotations

import json
import logging
from contextlib import closing
from pathlib import Path

from .. import state_migration
from ..collection_store import connection, schema
from . import mode

log = logging.getLogger(__name__)

MULTI_HOST = "VAULT_STORE_MULTI_HOST"
NOT_SERVED = "VAULT_STORE_NOT_SERVED"
WRITE_PATH_PENDING = "VAULT_STORE_WRITE_PATH_PENDING"
PAGES_AT_RISK = "VAULT_STORE_PAGES_AT_RISK"

ROLLBACK = "`exomem store-mode rollback`"


def require_supported_lease(config) -> None:
    """Refuse a lease setup under which a store-mode vault could lose or fork its pages.

    The capability fence, the head exchange and takeover all live in the coordinator, so store mode
    needs one. The store is one SQLite file that no replication copies between hosts, so authority
    must stay on this host. A false refusal costs the owner a restart with the lease environment
    set; the check lives here so activation and serving read one answer.
    """
    from ..collection_store import admission

    if not config.enabled:
        raise admission.coordination_required("store mode")
    if config.coordinated_across_hosts:
        raise connection.CollectionStoreError(
            MULTI_HOST,
            "this vault's pages live in the collection store, which nothing copies between hosts, so writer "
            "authority must stay on this host: set EXOMEM_WRITER_LEASE_TOPOLOGY=single-host with a loopback "
            f"EXOMEM_WRITER_LEASE_URL, or run {ROLLBACK} to return the pages to files")


def require_servable(vault_root: Path) -> None:
    """Refuse to start a service on a store-mode vault: no build serves its pages from the store yet.

    It prevents a service from answering reads out of Markdown that the store has replaced. A false
    refusal needs a store-mode vault on a build that cannot serve it, and the owner's way out is the
    rollback this message names. A file-mode vault starts, and the crash state that ``marker_inconsistency``
    names is logged on the way: it is reachable after a crash, so it warns and never refuses.
    """
    if not mode.store_mode_active(vault_root):
        inconsistency = marker_inconsistency(vault_root)
        if inconsistency is not None:
            log.warning("%s", inconsistency)
        return
    from .. import writer_lease

    require_supported_lease(writer_lease.LeaseConfig.from_env())
    raise connection.CollectionStoreError(
        NOT_SERVED,
        f"this vault's pages live in the collection store and this build cannot serve them yet: run {ROLLBACK} "
        "to return them to files, or start a build that serves store mode")


def marker_inconsistency(vault_root: Path) -> str | None:
    """Name the crash state in which the manifest records store mode but the store does not hold it.

    Activation records the manifest id before its store commit and rollback drops it after its own, so
    a crash between the two leaves the id beside no store file, no ``store_mode`` row or a row that
    says file. ``store_mode_active`` reads that as file mode, and rerunning the activation or the
    rollback settles it, so this reports and never refuses. A vault without the id never opens its store.
    """
    recorded = state_migration.recorded_descriptor_ids(vault_root)
    if recorded is None or state_migration.VAULT_STORE_COMPATIBILITY_ID not in recorded:
        return None
    if mode.store_mode_active(vault_root):
        return None
    return (f"The vault state manifest records {state_migration.VAULT_STORE_COMPATIBILITY_ID} but the collection "
            f"store does not hold store mode, as an interrupted activation or rollback leaves it: this vault "
            f"serves its pages from files, and {ROLLBACK} settles the records")


def write_refusal(vault_root: Path):
    """The OpError that a Markdown write to a store-mode vault gets, or None for a file-mode vault.

    A write that reached disk would put bytes where the store holds the pages. Until the store takes page
    writes, every one refuses, and the first cause that applies names itself: the serving thread, a
    takeover still pending, a recorded divergence, then the unfinished write path.
    """
    if not mode.store_mode_active(vault_root):
        return None
    from ..cli_ops import OpError
    from ..collection_store import runtime

    root = Path(vault_root).resolve()
    server = runtime._SERVERS.get(root)
    if server is not None:
        if server.runtime is None:
            return server.unserved()
        pending = server.runtime._takeover
        if pending is not None:
            error = pending.error()
            return OpError(error.code, error.reason, "Writes resume once the takeover settles.")
    reason = _divergence(root)
    if reason is not None:
        return OpError(
            "COLLECTION_STORE_DIVERGED", reason, "The owner reconciles the store with maintain_memory mode collections-store-adopt-local.")
    return OpError(
        WRITE_PATH_PENDING,
        "this vault's pages live in the collection store, and this build cannot write them there yet",
        f"Run {ROLLBACK} to return the pages to files.")


def _divergence(vault_root: Path) -> str | None:
    """Why the store records divergence, or None when it records none: either record fences writes."""
    with closing(connection.open_reader(connection.store_path(vault_root))) as reader:
        found = dict(reader.execute(
            "SELECT key, value FROM store_meta WHERE key IN (?, ?)",
            (schema.META_REPLICA_DIVERGENCE, schema.META_VIEW_DIVERGED)))
    if schema.META_REPLICA_DIVERGENCE in found:
        return json.loads(found[schema.META_REPLICA_DIVERGENCE])["reason"]
    if found.get(schema.META_VIEW_DIVERGED) == "1":
        return "a view carries another store instance's stamp"
    return None


def refuse_page_loss(vault_root: Path, operation: str) -> None:
    """Refuse a step that would replace the live store with a copy that holds no pages.

    The reverse export, the takeover adopt and the staged migration all install a store built from
    collection tables alone. In store mode that deletes the pages, which only a rollback may move.
    """
    if mode.store_mode_active(vault_root):
        raise connection.CollectionStoreError(
            PAGES_AT_RISK,
            f"{operation} would replace this vault's collection store with a copy that holds no pages: "
            f"run {ROLLBACK} first")
