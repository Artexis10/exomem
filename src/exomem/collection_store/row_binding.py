"""Find the rows a row id may name, without reading the rest of the collection.

A row id is ``<head>.<version>``. The head hashes the item key, so it survives edits and cannot be
inverted into a key. The binder keeps each collection's head map for its current generation, reads the
matching rows' current versions in one statement, and keeps only the rows this caller may read, so a
withheld row binds exactly as an absent one (OpenSpec ``harden-records-import-and-row-access``, D4).
"""

from __future__ import annotations

from .. import page_ids
from . import tokens

#: The largest collection whose head map is kept. Above it the generic scan answers, as it did before.
ROW_HEAD_MAX_ROWS = 100_000
#: Collections whose head maps one writer handle keeps; the least recently bound one is dropped.
ROW_HEAD_COLLECTIONS = 4


def _heads(writer, cid: str) -> dict[str, list[int]] | None:
    """Each head's row ids at the collection's current generation, or None when it has too many rows."""
    stamp = writer.connection.execute(
        "SELECT generation,audit_head FROM collections WHERE collection_id=?", (cid,)
    ).fetchone()
    kept = writer.handle.row_heads
    cached = kept.get(cid)
    if cached is not None and cached[0] == tuple(stamp):
        kept[cid] = kept.pop(cid)  # most recently bound last
        return cached[1]
    heads: dict[str, list[int]] = {}
    rows = 0
    for key, row_id in writer.connection.execute(
        "SELECT item_key,row_id FROM items WHERE collection_id=?", (cid,)
    ):
        rows += 1
        if rows > ROW_HEAD_MAX_ROWS:
            return None
        heads.setdefault(page_ids.part_head(key), []).append(row_id)
    kept.pop(cid, None)
    kept[cid] = (tuple(stamp), heads)
    while len(kept) > ROW_HEAD_COLLECTIONS:
        del kept[next(iter(kept))]
    return heads


def readable_matches(writer, manifest, head: str) -> tuple[list[tuple[str, str]], str] | None:
    """The `(item_key, item_version)` of each readable row with this id head, and the query snapshot.

    None where only the generic scan can answer: a derived collection holds no row here, a caller
    limited to some fields sees public versions, and a very large collection keeps no head map.
    """
    operation = writer._operation
    if manifest.derived or not operation.field_plan(manifest).owner:
        return None
    cid = manifest.collection_id
    readable, snapshot = operation.row_release(cid)
    heads = _heads(writer, cid)
    if heads is None:
        return None
    candidates = heads.get(head, ())
    if not candidates:
        return [], snapshot
    marks = ",".join("?" * len(candidates))
    found = writer.connection.execute(
        f"SELECT row_id,item_key,row_version,payload_hash FROM items WHERE row_id IN ({marks})", candidates
    ).fetchall()
    return [
        (key, tokens.item_version(cid, key, version, payload_hash))
        for row_id, key, version, payload_hash in found
        if readable(row_id, key)
    ], snapshot
