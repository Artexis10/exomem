"""Release capabilities of the collection store (OpenSpec add-collection-query-engine S1.8).

``records-summary-v1`` is the first slice: creating a NEW summary collection, every
route on one, and preserved-source import. The S1.8 release enabled it through the release
process by the owner's decision, after a real-vault verification; the S1.6 and S1.7 gates
stay open (OpenSpec add-collection-query-engine, task S1.8). This is a repository-owned release fence,
as ``governance.projection_runtime`` keeps for projected serving: every host running
one release answers the same, so a vault restored on another host behaves as it did.
No environment variable, caller argument or vault state turns it on. Slice rollback
records a per-vault disable in the store that can only narrow it.

``collections-migrate-v1`` is the same kind of fence for moving existing file Records and
Planning collections into the store (OpenSpec move-structured-collections-to-sqlite
P1b.3). The GA gate (P3.6) added it to ``RELEASED``: ``exomem collections migrate`` and the
managed upgrade migrator run, and ``offline_migrations()`` declares the migration to the
supervisor.

``collection-views-v1`` fences edit-back of store collection views (the same change, §6):
the serving store thread turning an owner's edit of a view into a governed update or a
held correction, and publishing pending views without a summary release. The P2.8 gate
added it to ``RELEASED``; the per-vault rollback narrows it too.
"""

from __future__ import annotations

import threading

from .. import structured_collections as collections
from . import connection

RECORDS_SUMMARY_V1 = "records-summary-v1"
UNAVAILABLE = "RECORDS_SUMMARY_UNAVAILABLE"
COLLECTIONS_MIGRATE_V1 = "collections-migrate-v1"
MIGRATION_UNAVAILABLE = "COLLECTION_STORE_MIGRATION_UNAVAILABLE"
COLLECTION_VIEWS_V1 = "collection-views-v1"

#: The release capabilities this release enables: S1.8 added RECORDS_SUMMARY_V1, P2.8
#: COLLECTION_VIEWS_V1 and the P3.6 GA gate COLLECTIONS_MIGRATE_V1.
RELEASED: frozenset[str] = frozenset({RECORDS_SUMMARY_V1, COLLECTION_VIEWS_V1, COLLECTIONS_MIGRATE_V1})
# S1.8 defines one release capability and a one-way rollback, not a user vocabulary.
DISABLED_KEY = "records_summary_disabled"
DISABLED = "COLLECTION_STORE_DISABLED"


# The disable is a one-way insert, so a store file seen disabled stays disabled. A false is never kept:
# a rollback can still land, and `require_not_disabled` at each checkout stays the live gate. A process
# serves a handful of vaults, so a full cache is cleared, which only costs one more read each.
_DISABLED_SEEN_MAX = 64
_DISABLED_SEEN: set[tuple[str, int, int]] = set()
_DISABLED_SEEN_LOCK = threading.Lock()


def _store_identity(root, path):
    try:
        status = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return str(root), status.st_dev, status.st_ino


def disabled_seen(root) -> bool:
    """Whether this process already saw the store at ``root`` disabled; False means unknown, and nothing is opened."""
    identity = _store_identity(root, connection.store_path(root))
    with _DISABLED_SEEN_LOCK:
        return identity in _DISABLED_SEEN


def records_summary_disabled(root) -> bool:
    path = connection.store_path(root)
    identity = _store_identity(root, path)
    if identity is None:
        return False
    with _DISABLED_SEEN_LOCK:
        if identity in _DISABLED_SEEN:
            return True
    from .runtime import reader_pool

    # store_meta keeps this key in every schema version, so a store awaiting its writer's migration answers too.
    with connection.head_reader(path, reader_pool(root, path)) as reader:
        disabled = _disabled(reader)
    if disabled:
        with _DISABLED_SEEN_LOCK:
            if len(_DISABLED_SEEN) >= _DISABLED_SEEN_MAX:
                _DISABLED_SEEN.clear()
            _DISABLED_SEEN.add(identity)
    return disabled


def _disabled(conn) -> bool:
    return conn.execute("SELECT 1 FROM store_meta WHERE key=?", (DISABLED_KEY,)).fetchone() is not None


def require_not_disabled(root) -> None:
    if records_summary_disabled(root):
        raise connection.CollectionStoreError(DISABLED, "summary collection access is disabled for this vault")


def records_summary_enabled(root=None) -> bool:
    """Whether this release serves records-summary-v1; the one place that answers it."""
    return RECORDS_SUMMARY_V1 in RELEASED and (root is None or not records_summary_disabled(root))


def collection_views_enabled(root=None, *, conn=None) -> bool:
    """Whether this release runs edit-back of store collection views; the one place that answers it.

    It prevents an unreleased edit-back from committing an owner's edit, or holding it, on a
    production store. Firing wrongly costs nothing new: views keep publishing as before, and
    an edit waits on disk until the release turns edit-back on. ``conn`` reads the rollback
    inside a transaction the caller already holds.
    """
    if COLLECTION_VIEWS_V1 not in RELEASED:
        return False
    if conn is not None:
        return not _disabled(conn)
    return root is None or not records_summary_disabled(root)


def views_published(root=None) -> bool:
    """Whether the serving store publishes pending views, between requests and on quiesce.

    Either release lets it: edit-back's, or the summary slice's, whose pages publish so.
    """
    return collection_views_enabled(root) or records_summary_enabled(root)


def summary_route_open(vault_root, manifest) -> bool:
    """Whether this release serves a request on ``manifest``'s collection: always, unless it is summary."""
    return manifest.view_mode != "summary" or records_summary_enabled(vault_root)


def require_records_summary(root=None) -> None:
    """Refuse a records-summary-v1 route with a named code while the release keeps it off."""
    if root is not None:
        require_not_disabled(root)
    if not records_summary_enabled():
        raise collections.CollectionError(
            UNAVAILABLE,
            "summary collections and import are unavailable: this release has not enabled "
            f"{RECORDS_SUMMARY_V1}",
            {"capability": RECORDS_SUMMARY_V1},
        )


def require_records_summary_route(vault_root, writer, action, values, *, prove=None) -> None:
    """Refuse the slice's routes on a production session while the release keeps it off.

    Those are an import, whatever its target, and every request on a store-routed summary
    collection. The collection is read under the caller's own authority first, so a
    collection it cannot see refuses as it would anyway; items-mode store collections
    and file collections pass unchanged.
    """
    if records_summary_enabled(vault_root):
        return
    if action == "import":
        require_records_summary(vault_root)
    selector = values.get("collection")
    from .preview import selected_writer

    if selector is None or selected_writer(vault_root, selector, prove=prove) is None:
        return
    with writer.read_collection(selector) as manifest:
        open_ = summary_route_open(vault_root, manifest)
    if not open_:
        require_records_summary(vault_root)


def collections_migrate_enabled() -> bool:
    """Whether this release runs collections-migrate-v1; the one place that answers it."""
    return COLLECTIONS_MIGRATE_V1 in RELEASED


def offline_migrations() -> tuple[str, ...]:
    """The offline migrations this release's managed migrator runs, as the candidate probe declares them.

    A supervisor of an earlier or later release reads this from the candidate interpreter, so
    only the release that will run the migration decides whether a handoff may run it.
    """
    return (COLLECTIONS_MIGRATE_V1,) if collections_migrate_enabled() else ()


def require_collections_migrate() -> None:
    """Refuse every migration step while the release keeps collections-migrate-v1 off.

    It prevents an unreleased migration from moving a vault's collections out of files,
    which only the reverse export (P1b.4) could undo. Firing wrongly costs nothing: it is
    a release constant, the same on every host, and file collections keep working.
    """
    if not collections_migrate_enabled():
        raise connection.CollectionStoreError(
            MIGRATION_UNAVAILABLE,
            f"moving file collections into the store is unavailable: this release has not enabled "
            f"{COLLECTIONS_MIGRATE_V1}")
