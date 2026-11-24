"""A store collection's shape history: one append-only row per entry, bound into the chain by its revise event.

The revise that commits an entry carries the entry's digest as its event effect (`history.effects`), so a dropped
or altered row fails verification. Every read here is one statement however many collections it names, and each
reads one range of the `(collection_id, committed_at, txn_id)` index per collection.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from .. import records, restructure


def encode(entry: Mapping[str, Any]) -> str:
    """The exact text the row stores for `entry`, and so the text its digest covers."""
    return records._canonical_json(dict(entry)).decode("utf-8")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write(execute, entry: Mapping[str, Any], text: str, *, txn_id: int) -> None:
    """Insert `entry`, encoded as `text`, for the revise `txn_id`, inside that revise's transaction."""
    execute(
        "INSERT INTO collection_shape_history"
        "(entry_id,collection_id,txn_id,committed_at,origin,undoes,adds_only,entry_json) VALUES (?,?,?,?,?,?,?,?)",
        (entry["id"], entry["collection_id"], txn_id, entry["at"], entry["origin"], entry["undoes"],
         int(restructure.adds_only(entry["steps"])), text),
    )


def bound_digest(conn, txn_id: int) -> str | None:
    """The digest of the entry that the revise `txn_id` committed, or None when it wrote none."""
    found = conn.execute("SELECT entry_json FROM collection_shape_history WHERE txn_id=?", (txn_id,)).fetchone()
    return None if found is None else digest(found[0])


def of_request(conn, request_id: str) -> dict[str, Any] | None:
    """The entry that the request `request_id` committed, or None when it committed none."""
    found = conn.execute(
        "SELECT h.entry_json FROM txns t JOIN collection_shape_history h ON h.txn_id=t.txn_id WHERE t.request_id=?",
        (request_id,)).fetchone()
    return None if found is None else json.loads(found[0])


def entries(conn, collection_id: str) -> list[dict[str, Any]]:
    """Every entry of `collection_id`, newest first; the same second is ordered by its transaction."""
    return [json.loads(text) for (text,) in conn.execute(
        "SELECT entry_json FROM collection_shape_history WHERE collection_id=? ORDER BY committed_at DESC, txn_id DESC",
        (collection_id,))]


# One keyset read: the entries older than a position, newest first, from one range of the (collection, time, txn)
# index. The extra row says whether a further page exists, so no page reads the entries beyond it.
FIRST_PAGE_SQL = (
    "SELECT entry_json FROM collection_shape_history WHERE collection_id=:cid "
    "ORDER BY committed_at DESC, txn_id DESC LIMIT :fetch"
)
NEXT_PAGE_SQL = (
    "SELECT entry_json FROM collection_shape_history WHERE collection_id=:cid AND (committed_at, txn_id) < (:at, :txn) "
    "ORDER BY committed_at DESC, txn_id DESC LIMIT :fetch"
)


def page(conn, collection_id: str, *, limit: int | None, after: str | None, collection: str) -> dict[str, Any]:
    """A page of `collection_id`'s entries older than the entry `after`, as `restructure.page` shapes it.

    The page is a keyset over `collection_shape_history_by_time`: its cost follows `limit`, not the history's length.
    The total is a count over that index alone and reads no entry.
    """
    limit = restructure.page_limit(limit)
    bound: dict[str, Any] = {"cid": collection_id, "fetch": limit + 1}
    if after is not None:
        found = conn.execute(
            "SELECT committed_at, txn_id FROM collection_shape_history WHERE entry_id=? AND collection_id=?",
            (after, collection_id)).fetchone()
        if found is None:
            raise restructure.not_found_after()
        bound["at"], bound["txn"] = found
    rows = [json.loads(text) for (text,) in conn.execute(FIRST_PAGE_SQL if after is None else NEXT_PAGE_SQL, bound)]
    fitted = restructure.shown(rows[:limit], collection)
    result: dict[str, Any] = {"entries": fitted, "total": conn.execute(
        "SELECT COUNT(*) FROM collection_shape_history WHERE collection_id=?", (collection_id,)).fetchone()[0]}
    if len(fitted) < len(rows):
        result["continuation"] = fitted[-1]["id"]
    return result


def newest(conn, collection_ids: Sequence[str], *, limit: int) -> dict[str, dict[str, Any]]:
    """For each collection, its `limit` newest entries, newest first, and its total, in one statement."""
    found: dict[str, dict[str, Any]] = {cid: {"entries": [], "total": 0} for cid in collection_ids}
    rows = conn.execute(
        "WITH wanted(collection_id) AS (SELECT DISTINCT value FROM json_each(:ids)) "
        "SELECT w.collection_id, (SELECT COUNT(*) FROM collection_shape_history c WHERE c.collection_id=w.collection_id), "
        "h.entry_json, h.committed_at, h.txn_id FROM wanted w "
        # The join names entry ids alone, so each newest entry is one primary-key lookup, not a scan of its collection.
        "LEFT JOIN collection_shape_history h ON h.entry_id IN ("
        "SELECT x.entry_id FROM collection_shape_history x WHERE x.collection_id=w.collection_id "
        "ORDER BY x.committed_at DESC, x.txn_id DESC LIMIT :limit)",
        {"ids": json.dumps(list(collection_ids)), "limit": limit}).fetchall()
    ordered = sorted(rows, key=lambda row: (row[3] or "", row[4] or 0), reverse=True)
    for cid, total, text, _at, _txn in ordered:
        found[cid]["total"] = total
        if text is not None:
            found[cid]["entries"].append(json.loads(text))
    return found


def counted_since(conn, collection_ids: Sequence[str], *, since: str, origin: str) -> dict[str, int]:
    """For each collection, its entries of `origin` from `since` on that the daily bound counts, in one statement.

    An undo and an entry that only adds fields are not counted, as the automatic-restructure bound states.
    """
    rows = conn.execute(
        "WITH wanted(collection_id) AS (SELECT DISTINCT value FROM json_each(:ids)) "
        "SELECT w.collection_id, COUNT(h.entry_id) FROM wanted w "
        "LEFT JOIN collection_shape_history h ON h.collection_id=w.collection_id AND h.committed_at>=:since "
        "AND h.origin=:origin AND h.undoes IS NULL AND h.adds_only=0 GROUP BY w.collection_id",
        {"ids": json.dumps(list(collection_ids)), "since": since, "origin": origin}).fetchall()
    return {cid: count for cid, count in rows}
