"""The read-only collections inventory: every collection the store and the manifest census find.

OpenSpec move-structured-collections-to-sqlite C0.1. The collections-store preflight
answers for the file collections; this module adds one row for each collection in the
vault's store, read from the store itself, and pages the merged list by manifest path.
A row holds identities, counts, statuses and codes, never row values. A count that is
unknown or does not apply is ``None``, never 0.
"""

from __future__ import annotations

from contextlib import ExitStack

from .. import structured_collections as collections
from . import connection, derived_rows, types

#: Rows per response, so that a vault with many collections never makes one huge result.
PAGE_SIZE = 100
STORE, FILE = "store", "file"
_CONFLICT = "COLLECTION_STORE_MARKER_CONFLICT"
_STORE_FAILURES = (connection.CollectionStoreError, connection.CollectionStoreUnavailable)
_DETAIL = (
    "SELECT c.type_name,c.view_mode,c.derived,c.legacy_audit_status,m.manifest_text,"
    "(SELECT COUNT(*) FROM items i WHERE i.collection_id=c.collection_id),"
    "(SELECT COUNT(*) FROM projection_state p WHERE p.collection_id=c.collection_id AND p.kind='item' "
    "AND p.published_sha256 IS NOT NULL),"
    "(SELECT COUNT(*) FROM held_candidates h WHERE h.collection_id=c.collection_id) "
    "FROM collections c JOIN collection_manifests m "
    "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version WHERE c.collection_id=?")


def page(root, file_rows, proof, after=None) -> dict:
    """One page of the inventory after the ``position`` of a cursor, with ``next_cursor`` for the rest.

    ``file_rows`` are the preflight's rows; ``proof`` is the ownership proof they were found
    under. The store is read only when the vault's marker exists, because a store without a
    marker serves no collection.
    """
    with ExitStack() as stack:
        conn, failure = None, None
        if proof.marker is not None:
            try:
                conn = stack.enter_context(connection.reader_snapshot(connection.store_path(root)))
            except _STORE_FAILURES as error:
                failure = {"code": error.code, "reason": error.reason}
        stored = {} if conn is None else dict(conn.execute("SELECT collection_id,manifest_path FROM collections"))
        routed = {entry["collection_id"]: entry["manifest_path"] for entry in proof.entries}
        entries = [(path, STORE, collection_id) for collection_id, path in {**routed, **stored}.items()]
        entries.extend((row["path"], FILE, row) for row in file_rows)
        entries.sort(key=lambda entry: entry[:2])
        remaining = [entry for entry in entries if after is None or entry[:2] > after]
        shown = remaining[:PAGE_SIZE]
        rows = [_file_row(found) if authority == FILE else
                _store_row(root, conn, found, path, failure, routed=found in routed, stored=found in stored)
                for path, authority, found in shown]
    return {"collections": rows, "page_size": PAGE_SIZE, "total": len(entries),
            "next_cursor": _cursor(shown[-1][:2]) if len(remaining) > PAGE_SIZE else None}


def _store_row(root, conn, collection_id, path, failure, *, routed, stored) -> dict:
    row = _empty(collection_id, path, STORE)
    if failure is not None:
        return {**row, "blocker": failure}
    if not stored:
        return {**row, "blocker": {"code": _CONFLICT,
                                   "reason": "the vault's marker routes this collection, but its store does not hold it"}}
    type_name, view_mode, derived, audit, text, items, views, held = conn.execute(_DETAIL, (collection_id,)).fetchone()
    row.update(type=type_name, view_mode=view_mode, legacy_audit_status=audit, item_views=views, held=held,
               # A derived collection's rows live beside the store; its import log holds their count.
               rows=derived_rows.logged(conn, collection_id)[1] if derived else items)
    try:
        manifest = collections.parse_manifest_bytes(root, path, text.encode())
    except collections.CollectionError as error:
        row["blocker"] = {"code": error.code, "reason": error.reason}
    else:
        row.update(semantic_profile=manifest.semantic_profile, storage_strategy=manifest.storage.strategy)
    if not routed:
        row["blocker"] = {"code": _CONFLICT, "reason": "the vault's marker does not route this store collection, "
                                                       "so no request reaches it"}
    return row


def _file_row(found) -> dict:
    row = _empty(found["collection_id"], found["path"], FILE)
    strategy = found["layout"]
    row.update(semantic_profile=found["semantic_profile"], storage_strategy=strategy,
               view_mode=found.get("view_mode"), rows=found.get("rows"),
               legacy_audit_status=found.get("legacy_audit_status"), stamped_items=found.get("stamped_items"),
               blocker=found.get("blocker"))
    if found["semantic_profile"] is not None:
        try:
            row["type"] = types.type_for_profile(found["semantic_profile"]).name
        except types.CollectionTypeError:
            pass  # An unregistered type stays unknown.
    if row["rows"] is not None and strategy in collections.MARKDOWN_ROW_STORAGE:
        # The file reader makes one row per item file, and a log holds its rows in one page.
        row["item_views"] = row["rows"] if strategy == "markdown-items" else 0
    return row


def _empty(collection_id, path, authority) -> dict:
    return {"collection_id": collection_id, "path": path, "authority": authority, "type": None,
            "semantic_profile": None, "storage_strategy": None, "view_mode": None, "rows": None,
            "item_views": None, "legacy_audit_status": None, "held": None, "stamped_items": None, "blocker": None}


def _cursor(position) -> str:
    # Plain text, because the egress filter withholds an opaque token as a possible credential.
    path, authority = position
    return f"{authority}:{path}"


def position(cursor):
    """Where a ``next_cursor`` resumes; ``None`` for the first page. A foreign cursor is refused."""
    if cursor is None:
        return None
    authority, separator, path = str(cursor).partition(":")
    if separator and path and authority in (STORE, FILE):
        return path, authority
    raise ValueError("INVALID_ARGUMENTS: cursor is not a next_cursor of the collections-store inventory; "
                     "omit it to read the first page")
