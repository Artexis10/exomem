"""What a vault in store mode refuses while nothing can serve its pages (OpenSpec make-vault-store-canonical, task 1.6).

A store-mode vault keeps its pages in the collection store, so each path that would serve stale
Markdown, copy pages to another host, or swap the live store for a copy without pages must refuse.
A file-mode vault takes none of these branches: every answer starts from ``mode.store_mode_active``,
which reads the manifest first and never opens the store of a file-mode vault.
"""

from __future__ import annotations

import logging
from contextlib import closing
from pathlib import Path

from .. import state_migration
from ..collection_store import connection, schema
from . import mode

log = logging.getLogger(__name__)

MULTI_HOST = "VAULT_STORE_MULTI_HOST"
NOT_SERVED = "VAULT_STORE_NOT_SERVED"
OFF_THREAD = "VAULT_STORE_OFF_THREAD"
SERVICE_REQUIRED = "VAULT_STORE_SERVICE_REQUIRED"
PAGES_AT_RISK = "VAULT_STORE_PAGES_AT_RISK"
STORE_ELSEWHERE = "VAULT_STORE_ELSEWHERE"
WRITER_UNROUTED = "VAULT_STORE_WRITER_UNROUTED"

ROLLBACK = "`exomem store-mode rollback`"
ELSEWHERE_REASON = (
    "The vault's replica says another state root holds this vault's pages in the collection store, "
    "so a file-mode service here would write Markdown beside them."
)


def elsewhere_remedy(order) -> str:
    """What the owner does about ``VAULT_STORE_ELSEWHERE``: the release command and the claim id.

    ``order`` is what the replica's marker says (``marker.Order``). A decision names the claim to
    release. A store bit with no decision is a claim from a build older than the register, which
    ``--claim legacy`` releases. A decision that this build cannot read has no id to name, so the
    owner needs a build that reads it, or the holder's rollback.
    """
    from .marker import LEGACY_CLAIM

    start = (
        f"Start the service on the state root that holds the store, or run {ROLLBACK} there and "
        "let the replica sync back to this machine."
    )
    if not order.known:
        return (
            f"{start} This replica's store-mode decision is in a form that this build cannot read, "
            "so `exomem store-mode release` is not available: use a build that reads it."
        )
    claim = LEGACY_CLAIM if order.decision is None else order.decision.id
    return (
        f"{start} If that root is gone, stop every service, set "
        "EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN and run "
        f"`exomem store-mode release --apply --claim {claim} --why REASON` on this root."
    )


def elsewhere_error(order) -> connection.CollectionStoreError:
    """The refusal for a root that another root's store claim stands against."""
    return connection.CollectionStoreError(
        STORE_ELSEWHERE, f"{ELSEWHERE_REASON} {elsewhere_remedy(order)}"
    )


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
    rollback this message names. A file-mode vault starts, and the crash state that
    ``marker_inconsistency`` names is logged on the way: it is reachable after a crash, so it warns
    and never refuses. The one refusal of a file-mode vault is ``require_no_store_elsewhere``: its
    replica says another state root holds the store.
    """
    if not mode.store_mode_active(vault_root):
        inconsistency = marker_inconsistency(vault_root)
        if inconsistency is not None:
            log.warning("%s", inconsistency)
        require_no_store_elsewhere(vault_root)
        return
    from .. import writer_lease

    require_supported_lease(writer_lease.LeaseConfig.from_env())
    raise connection.CollectionStoreError(
        NOT_SERVED,
        f"this vault's pages live in the collection store and this build cannot serve them yet: run {ROLLBACK} "
        "to return them to files, or start a build that serves store mode")


def require_no_store_elsewhere(vault_root: Path) -> None:
    """Refuse to start a file-mode service on a vault that another state root holds in store mode.

    It prevents this service from writing Markdown beside pages that the other root's store owns. A
    false refusal needs a replica that says store after the owner rolled back and before the new
    replica arrives, and the owner's way out is to let the replica sync, to run the rollback's
    hand-back again or, when the holder is gone, to release its claim. Only a readable replica that
    says store refuses: an absent or unreadable one is a normal state of a synced vault.
    """
    from . import marker

    found = marker.conflict(vault_root)
    if found is not None:
        raise elsewhere_error(found.order)


def marker_inconsistency(vault_root: Path) -> str | None:
    """Name the crash state in which the manifest records store mode but the store does not hold it.

    Activation records the manifest id before its store commit and rollback drops it after its own, so
    a crash between the two leaves the id beside no store file, no ``store_mode`` row or a row that
    says file. ``store_mode_active`` reads that as file mode, and rerunning the activation or the
    rollback settles it, so this reports and never refuses. A vault without the id never opens its store.
    A manifest that cannot be read is no such state: ``StateMigrationManifestError`` surfaces to the caller.
    """
    if not state_migration.vault_store_recorded(vault_root):
        return None
    if mode.store_mode_active(vault_root):
        return None
    return (f"The vault state manifest records {state_migration.VAULT_STORE_COMPATIBILITY_ID} but the collection "
            f"store does not hold store mode, as an interrupted activation or rollback leaves it: this vault "
            f"serves its pages from files, and {ROLLBACK} settles the records")


def page_writer(vault_root: Path):
    """The collection store writer that a governed batch commits through; None for file mode.

    A store-mode vault never gets a Markdown write: a write that reached disk would put bytes where
    the store holds the pages, so this raises the first cause that applies: the serving thread, a
    takeover still pending, a recorded divergence, then a write that did not come through the store
    thread.
    """
    in_store_mode, refusal = _write_state(vault_root)
    if refusal is not None:
        raise refusal
    if not in_store_mode:
        return None
    from ..collection_store.preview import bound_writer

    return bound_writer(Path(vault_root).resolve())


def _write_state(vault_root: Path):
    """Whether the vault is in store mode, and the refusal that a write to it gets."""
    if not mode.store_mode_active(vault_root):
        return False, None
    from ..cli_ops import OpError
    from ..collection_store import runtime
    from ..collection_store.preview import bound_writer

    root = Path(vault_root).resolve()
    server = runtime._SERVERS.get(root)
    if server is not None:
        if server.runtime is None:
            return True, server.unserved()
        pending = server.runtime._takeover
        if pending is not None:
            error = pending.error()
            return True, OpError(
                error.code, error.reason, "Writes resume once the takeover settles."
            )
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        reason = schema.divergence_reason(reader)
    if reason is not None:
        return True, OpError(
            "COLLECTION_STORE_DIVERGED",
            reason,
            "The owner reconciles the store with maintain_memory "
            "mode collections-store-adopt-local.",
        )
    if bound_writer(root) is None:
        # Waiting for the store thread from here, under the mutation boundary, would deadlock it; no
        # caller may reach the store from another thread, so this is an invariant, not a retry.
        return True, OpError(
            OFF_THREAD,
            "this write reached a store-mode vault outside the thread that serves its store",
            "Send the request to the service that serves this vault. "
            f"Run {ROLLBACK} to return the pages to files.",
        )
    return True, None


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


def unrouted(operation: str, why: str):
    """The refusal of a governed write with no store form yet, raised before it changes anything.

    It prevents a write that would put bytes on disk where the store holds the pages: the importer
    would then adopt them as an outside edit, and the store and the files would disagree. A false
    refusal costs the owner one rollback to run that single operation on files, and the message
    names the operation and the reason so the owner can tell a gap from a mistake.
    """
    from ..cli_ops import OpError

    return OpError(
        WRITER_UNROUTED,
        f"{operation} cannot change a store-mode vault yet: {why}",
        f"Run {ROLLBACK} to return the pages to files, repeat {operation}, then migrate again.",
    )
