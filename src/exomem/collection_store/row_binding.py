"""Find the rows a row id may name, without reading the rest of the collection.

A row id is ``<head>.<version>``. The head hashes the item key, so it survives edits and cannot be
inverted into a key. The binder keeps each collection's head map for its current generation, reads the
matching rows' current versions in one statement, and keeps only the rows this caller may read, so a
withheld row binds exactly as an absent one (OpenSpec ``harden-records-import-and-row-access``, D4).
"""

from __future__ import annotations

from array import array

from .. import page_ids
from . import tokens

#: The largest collection whose head map is kept. Above it the generic scan answers, as it did before.
ROW_HEAD_MAX_ROWS = 100_000
#: Collections whose head maps one writer handle keeps; the least recently bound one is dropped.
ROW_HEAD_COLLECTIONS = 4

#: A head map: each row's head number beside its row id, about 16 bytes a row. At the row ceiling a
#: lookup scans it in about 2 ms, which a sorted or hashed form would buy with slower rebuilds or more memory.
Heads = tuple[array, array]


def _heads(writer, cid: str) -> Heads | None:
    """The collection's head map at its current generation, or None when it has too many rows.

    A head number is the process's own ``hash`` of the head: the map lives only in this process, and
    ``readable_matches`` re-checks each found row's head, so two heads that share a number still differ.
    """
    stamp = tuple(writer.connection.execute(
        "SELECT generation,audit_head FROM collections WHERE collection_id=?", (cid,)
    ).fetchone())
    kept = writer.handle.row_heads
    cached = kept.get(cid)
    if cached is not None and cached[0] == stamp:
        kept[cid] = kept.pop(cid)  # most recently bound last
        return cached[1]
    heads: Heads | None = (array("q"), array("q"))
    numbers, row_ids = heads
    for key, row_id in writer.connection.execute(
        "SELECT item_key,row_id FROM items WHERE collection_id=?", (cid,)
    ):
        if len(numbers) == ROW_HEAD_MAX_ROWS:
            heads = None  # kept as this generation's verdict, so the next bind does not count the rows again
            break
        numbers.append(hash(page_ids.part_head(key)))
        row_ids.append(row_id)
    kept.pop(cid, None)
    kept[cid] = (stamp, heads)
    while len(kept) > ROW_HEAD_COLLECTIONS:
        del kept[next(iter(kept))]
    return heads


def _candidates(heads: Heads, head: str) -> list[int]:
    """The row ids whose head number is this head's."""
    numbers, row_ids = heads
    number, found, at = hash(head), [], 0
    while True:
        try:
            at = numbers.index(number, at)
        except ValueError:
            return found
        found.append(row_ids[at])
        at += 1


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
    candidates = _candidates(heads, head)
    if not candidates:
        return [], snapshot
    marks = ",".join("?" * len(candidates))
    found = writer.connection.execute(
        f"SELECT row_id,item_key,row_version,payload_hash FROM items WHERE row_id IN ({marks})", candidates
    ).fetchall()
    return [
        (key, tokens.item_version(cid, key, version, payload_hash))
        for row_id, key, version, payload_hash in found
        if page_ids.part_head(key) == head and readable(row_id, key)
    ], snapshot
