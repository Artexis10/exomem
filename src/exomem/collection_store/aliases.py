"""Retired item paths of converted collections (design §17, OpenSpec C2.4).

Converting a collection to summary mode removes its item files and keeps each row's former view
path in ``items.view_path`` as that row's retired alias (``CollectionWriter.convert_to_summary``).
A read of a retired path answers the stored row as the collection's governed query releases it,
read-only and labelled as a store row. Nothing writes the file back. A wikilink that names a retired
path resolves through ``link_target`` to the same row.
"""

from __future__ import annotations

from .. import collection_profiles
from .. import structured_collections as collections
from .connection import CollectionStoreError
from .preview import bound_writer, canonical_read
from .summary import SUMMARY

#: The row of a summary collection that keeps ``path`` as its retired alias.
_RETIRED = ("SELECT i.collection_id,i.item_key FROM items i JOIN collections c ON c.collection_id=i.collection_id "
            "WHERE i.view_path=? AND c.view_mode=?")


def retired(conn, path):
    """``(collection_id, item_key)`` of the row whose retired alias is ``path``, or None."""
    return conn.execute(_RETIRED, (path, SUMMARY)).fetchone()


@canonical_read(projection=True)
def read(vault_root, path):
    """The stored row behind the retired item path ``path``, or None.

    None also when the caller may not read that row, so a withheld row reads as a missing file.
    """
    from .. import record_governance

    writer = bound_writer(vault_root)
    found = None if writer is None else retired(writer.connection, path)
    if found is None:
        return None
    cid, key = found
    try:
        with writer.read_collection(cid) as manifest:
            profile = collection_profiles.profile_for(manifest.semantic_profile)
            result = record_governance.query_collection(
                vault_root, manifest, semantic_profile=manifest.semantic_profile,
                filters=[{"column": profile.item_id_property, "op": "eq", "value": key}], limit=1)
    except collections.CollectionError as error:
        if error.code == "COLLECTION_NOT_FOUND":
            return None
        raise
    if not result.rows:
        return None
    return {
        "outcome": (f"This item file was retired when its collection moved to the collection store. "
                    f"This is its stored row, read-only; change it with {profile.tool}."),
        "path": path,
        "store_row": result.rows[0],
        "collection": manifest.path,
        "collection_id": cid,
        "source": "collection store",
        "read_only": True,
    }


def read_named(vault_root, path):
    """``read`` for every spelling of ``path`` that a direct page read accepts, or None.

    Those spellings drop the Knowledge Base prefix, the ``.md`` suffix or both, as a wikilink does.
    """
    from .. import get_page

    for spelling in get_page.spellings(path):
        if (row := read(vault_root, spelling)) is not None:
            return row
    return None


def link_target(vault_root, target):
    """The retired item path that the wikilink path ``target`` names, or None.

    None also when the caller may not read the row, so a withheld row reads as a missing page.
    """
    from ..cli_ops import OpError

    try:
        row = read_named(vault_root, target)
    except (OpError, CollectionStoreError):
        # A store that this process cannot reach leaves the link a forward reference, as before conversion.
        return None
    return None if row is None else row["path"]
