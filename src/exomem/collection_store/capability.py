"""Release capabilities of the collection store (OpenSpec add-collection-query-engine S1.8).

``records-summary-v1`` is the first slice: creating a NEW summary collection, every
route on one, and preserved-source import. It stays off until the S1.8 release adds it
to ``RELEASED`` through the release process. This is a repository-owned release fence,
as ``governance.projection_runtime`` keeps for projected serving: every host running
one release answers the same, so a vault restored on another host behaves as it did.
No environment variable, caller argument or vault state turns it on. Slice rollback
records a per-vault disable in the store that can only narrow it.
"""

from __future__ import annotations

from contextlib import closing

from .. import structured_collections as collections
from . import connection

RECORDS_SUMMARY_V1 = "records-summary-v1"
UNAVAILABLE = "RECORDS_SUMMARY_UNAVAILABLE"

#: The release capabilities this release enables. S1.8 adds RECORDS_SUMMARY_V1.
RELEASED: frozenset[str] = frozenset()
# S1.8 defines one release capability and a one-way rollback, not a user vocabulary.
DISABLED_KEY = "records_summary_disabled"
DISABLED = "COLLECTION_STORE_DISABLED"


def records_summary_disabled(root) -> bool:
    path = connection.store_path(root)
    if not path.exists():
        return False
    with closing(connection.open_reader(path)) as reader:
        return reader.execute("SELECT 1 FROM store_meta WHERE key=?", (DISABLED_KEY,)).fetchone() is not None


def require_not_disabled(root) -> None:
    if records_summary_disabled(root):
        raise connection.CollectionStoreError(DISABLED, "summary collection access is disabled for this vault")


def records_summary_enabled(root=None) -> bool:
    """Whether this release serves records-summary-v1; the one place that answers it."""
    return RECORDS_SUMMARY_V1 in RELEASED and (root is None or not records_summary_disabled(root))


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


def require_records_summary_route(vault_root, writer, action, values) -> None:
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

    if selector is None or selected_writer(vault_root, selector) is None:
        return
    with writer.read_collection(selector) as manifest:
        summary = manifest.view_mode == "summary"
    if summary:
        require_records_summary(vault_root)
