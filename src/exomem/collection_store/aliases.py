"""Retired item paths of converted collections (design §17, OpenSpec C2.4).

Converting a collection to summary mode removes its item files and keeps each row's former view
path in ``items.view_path`` as that row's retired alias (``CollectionWriter.convert_to_summary``).
A read of a retired path answers the stored row as the collection's governed query releases it,
with its body for a reader of the whole row, read-only and labelled as a store row. Nothing writes
the file back. A wikilink that names a retired path resolves through ``link_target`` to the same row,
and the links of one query resolve through one ``LinkTargets``, which reads each collection once.
"""

from __future__ import annotations

from contextvars import ContextVar

from .. import collection_profiles
from .. import structured_collections as collections
from .connection import CollectionStoreError
from .preview import bound_writer, canonical_read
from .summary import SUMMARY

#: Set while a link check reads rows. A query that runs inside that read leaves its own links to
#: retired paths unresolved: the check answers only whether a row is released, so this bounds the
#: nesting at one row, even where rows link to each other.
_CHECKING_LINK = ContextVar("exomem_retired_link_check", default=False)
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
            body = writer.row_body(cid, key) if result.rows else {}
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
        **body,
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

    None also when the caller may not read the row, so a withheld row reads as a missing page. A
    caller that resolves many links resolves them through one ``LinkTargets`` instead.
    """
    return LinkTargets(vault_root)(target)


class LinkTargets:
    """``link_target`` for the links of one query: each linked collection's rows are read once.

    The keys that it keeps hold for one caller and one read snapshot, so a caller makes one for a
    query and drops it with that query.
    """

    def __init__(self, vault_root):
        self.root = vault_root
        #: For each collection read, the keys of the rows that the caller may read.
        self.released = {}

    def __call__(self, target):
        """The retired item path that the wikilink path ``target`` names, or None.

        None also when the caller may not read the row, so a withheld row reads as a missing page.
        """
        from ..cli_ops import OpError

        if _CHECKING_LINK.get():
            return None
        checking = _CHECKING_LINK.set(True)
        try:
            return _link_target(self.root, target, self.released)
        except (OpError, CollectionStoreError, collections.CollectionError):
            # A store or row this process cannot read leaves the link a forward reference, as before conversion.
            return None
        finally:
            _CHECKING_LINK.reset(checking)


@canonical_read
def _link_target(vault_root, target, released):
    """``LinkTargets`` under one read snapshot, keeping each collection's released keys in ``released``."""
    from .. import get_page

    writer = bound_writer(vault_root)
    if writer is None:
        # A process that does not serve the store reads the path through the service that does.
        row = read_named(vault_root, target)
        return None if row is None else row["path"]
    for spelling in get_page.spellings(target):
        found = retired(writer.connection, spelling)
        if found is None:
            continue
        cid, key = found
        if cid not in released:
            released[cid] = _released_keys(writer, cid)
        if key in released[cid]:
            return spelling
    return None


def _released_keys(writer, cid):
    """The keys of the rows of collection ``cid`` that the caller may read, or none if it cannot read them.

    One read of the collection's governed rows: the row release that its query and ``read`` apply,
    without the query's shaping or the rows' own link checks.
    """
    from ..cli_ops import OpError

    try:
        with writer.read_collection(cid):
            rows = writer._operation.authorized_rows(cid)[0]
    except (OpError, CollectionStoreError, collections.CollectionError):
        # Each link into a collection this process cannot read stays a forward reference.
        return frozenset()
    return frozenset(row["item_key"] for row in rows)
