"""The span index of one field of a Records collection: built on demand, kept current by each write.

A large timeline query asks for a field's index (`request`). The serving store thread seeds its state row
(`seed`) and builds it in batches of rows (`backfill`); until then the query is refused as warming. Each
transaction then replaces the spans of the rows it touched (`maintain`). Every entry comes from
`timeline.fold`, the function the direct path and the writes use, so the index holds what the versions say.
A store copy holds no state row, so its indexes warm again rather than trust copied entries.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from ..query_engine import scalars
from . import timeline, typed_storage

#: The format of `value_key`; a change makes every index rebuild.
KEY_FORMAT = 1
#: The tag of a stored value that does not fit the field's declared type: it equals no typed literal.
UNKEYED_TAG = 255
#: How many requests the in-memory set holds; a full set takes no new one until the next seed drains it.
_REQUEST_LIMIT = 64
_requests: dict[str, set[tuple[str, str]]] = {}
_requests_guard = threading.Lock()


def keyed(value: Any, kind: str) -> tuple[int, str]:
    """A span value's (tag, key) under the field's declared type, as a current filter compares it."""
    try:
        return scalars.scalar_key(value, kind)
    except scalars.ScalarValueError:
        return UNKEYED_TAG, typed_storage.canonical_json(value)


def encoding_hash(kind: str) -> str:
    """What the index entries of a field of this type depend on: the type and the key format."""
    return hashlib.sha256(json.dumps({"type": kind, "key_format": KEY_FORMAT}, sort_keys=True).encode()).hexdigest()


def indexable(manifest, field: str) -> str | None:
    """The declared scalar type of a top-level field, or None when the field has no index."""
    spec = manifest.schema.fields.get(field)
    return spec.type if spec is not None and spec.type in scalars.SCALAR_TYPES else None


def _store(store_path) -> str:
    return str(Path(store_path).resolve())


def request(store_path, collection_id: str, field: str) -> None:
    """Ask the serving store thread to build this field's index."""
    with _requests_guard:
        pending = _requests.setdefault(_store(store_path), set())
        if len(pending) < _REQUEST_LIMIT:
            pending.add((collection_id, field))


def requested(store_path) -> bool:
    with _requests_guard:
        return bool(_requests.get(_store(store_path)))


def take(store_path) -> list[tuple[str, str]]:
    """The pending requests of a store, which the caller seeds in its transaction."""
    with _requests_guard:
        return sorted(_requests.pop(_store(store_path), ()))


def state(conn, collection_id: str, field: str) -> tuple[int, str, int, bool] | None:
    """(field_key, encoding_hash, built_through, ready) of a field's index, or None when it has none."""
    found = conn.execute("SELECT field_key,encoding_hash,built_through,ready FROM field_span_state "
                         "WHERE collection_id=? AND field_path=?", (collection_id, field)).fetchone()
    return None if found is None else (found[0], found[1], found[2], bool(found[3]))


def ready(conn, manifest, field: str) -> int | None:
    """The field_key of a field's index that answers queries now, else None."""
    found = state(conn, manifest.collection_id, field)
    kind = indexable(manifest, field)
    if found is None or kind is None or not found[3] or found[1] != encoding_hash(kind):
        return None
    return found[0]


def progress(conn, collection_id: str, field: str) -> dict[str, int]:
    """How far a field's build has come, as a share of the collection's current rows.

    A share and not counts: the caller may not read every row, and a count would tell it how many exist.
    """
    found = state(conn, collection_id, field)
    total = conn.execute("SELECT count(*) FROM items WHERE collection_id=?", (collection_id,)).fetchone()[0]
    built = 0 if found is None else conn.execute(
        "SELECT count(*) FROM items WHERE collection_id=? AND row_id<=?", (collection_id, found[2])).fetchone()[0]
    return {"percent_built": 100 if not total else min(99, built * 100 // total)}


def seed(conn, manifest, field: str) -> None:
    """Give a requested field a warming state row, or reset one whose encoding is out of date.

    A field that is no longer a declared scalar gets none. An out-of-date index is one that an earlier key
    format built; a query cannot use it, and without this reset only a write to the collection would rebuild it.
    """
    kind = indexable(manifest, field)
    if kind is None:
        return
    found = state(conn, manifest.collection_id, field)
    if found is None:
        conn.execute("INSERT INTO field_span_state(collection_id,field_path,encoding_hash,built_through,ready) "
                     "VALUES(?,?,?,0,0)", (manifest.collection_id, field, encoding_hash(kind)))
    elif found[1] != encoding_hash(kind):
        _reset(conn, found[0], kind)


def folded(conn, field: str, kind: str, row_ids: Iterable[int]) -> Iterator[tuple[int, timeline.Span, int, str]]:
    """(row_id, span, tag, key) of each span of `field` in the current rows among `row_ids`, by row id.

    The direct query path and the index both take their spans from here, from one set-based read of the
    rows' versions, so the two cannot disagree on a span or on how its value compares.
    """
    current = [row_id for (row_id,) in conn.execute(
        "SELECT row_id FROM items WHERE row_id IN (SELECT value FROM json_each(?)) ORDER BY row_id",
        (json.dumps(sorted(row_ids)),))]
    found = timeline.histories(conn, current)
    for row_id in current:
        versions, facts = found[row_id]
        for span in timeline.spans(timeline.fold(versions, facts, fields=(field,)).live[field]):
            yield (row_id, span, *keyed(span.value, kind))


def _entries(conn, field_key: int, field: str, kind: str, row_ids: Iterable[int]) -> list[tuple]:
    """The index entries of the current rows among `row_ids`."""
    return [(field_key, tag, key, span.start.order[0], span.start.order[1],
             None if span.end is None else span.end.order[0], row_id)
            for row_id, span, tag, key in folded(conn, field, kind, row_ids)]


def _replace(conn, field_key: int, field: str, kind: str, row_ids: list[int]) -> None:
    """Replace the entries of `row_ids` through the writer's accounted `conn`; reads take its raw connection."""
    conn.execute("DELETE FROM field_spans WHERE field_key=? AND row_id IN (SELECT value FROM json_each(?))",
                 (field_key, json.dumps(row_ids)))
    entries = _entries(conn.connection, field_key, field, kind, row_ids)
    if entries:
        conn.executemany("INSERT INTO field_spans(field_key,value_tag,value_key,start_at,start_seq,end_at,row_id) "
                         "VALUES(?,?,?,?,?,?,?)", entries)


def _drop(conn, field_key: int) -> None:
    conn.execute("DELETE FROM field_spans WHERE field_key=?", (field_key,))
    conn.execute("DELETE FROM field_span_state WHERE field_key=?", (field_key,))


def _reset(conn, field_key: int, kind: str) -> None:
    conn.execute("DELETE FROM field_spans WHERE field_key=?", (field_key,))
    conn.execute("UPDATE field_span_state SET encoding_hash=?,built_through=0,ready=0 WHERE field_key=?",
                 (encoding_hash(kind), field_key))


def _reconciled(conn, manifest) -> list[tuple[int, str, str, int, bool]]:
    """The collection's indexes after the manifest: a removed field's is dropped, a retyped field's is reset."""
    kept = []
    for field_key, field, stored, built_through, is_ready in conn.execute(
            "SELECT field_key,field_path,encoding_hash,built_through,ready FROM field_span_state "
            "WHERE collection_id=? ORDER BY field_key", (manifest.collection_id,)).fetchall():
        kind = indexable(manifest, field)
        if kind is None:
            _drop(conn, field_key)
            continue
        if stored != encoding_hash(kind):
            _reset(conn, field_key, kind)
            built_through, is_ready = 0, False
        kept.append((field_key, field, kind, built_through, bool(is_ready)))
    return kept


def maintain(conn, manifest, txn_id: int) -> None:
    """Keep each index of the collection current after transaction `txn_id`.

    The rows the transaction touched are the rows of its audit effects. Each loses its spans, and a row that
    is current gets them again from its versions: a write replaces them, a retraction removes them and a
    re-admission rebuilds them. A warming index takes only the rows its build has passed.
    """
    indexes = _reconciled(conn, manifest)
    if not indexes:
        return
    touched = [row_id for (row_id,) in conn.execute(
        "SELECT DISTINCT row_id FROM audit_effects WHERE txn_id=? AND row_id IS NOT NULL", (txn_id,))]
    if not touched:
        return
    for field_key, field, kind, built_through, is_ready in indexes:
        rows = touched if is_ready else [row_id for row_id in touched if row_id <= built_through]
        if rows:
            _replace(conn, field_key, field, kind, rows)


def backfill(conn, manifest, *, limit: int) -> bool:
    """Build one warming index of the collection by up to `limit` rows past `built_through`.

    Returns True when this batch reached the last row and the index is ready.
    """
    pending = [index for index in _reconciled(conn, manifest) if not index[4]]
    if not pending:
        return False
    field_key, field, kind, built_through, _ = min(pending, key=lambda index: index[3])
    rows = [row_id for (row_id,) in conn.execute(
        "SELECT row_id FROM items WHERE collection_id=? AND row_id>? ORDER BY row_id LIMIT ?",
        (manifest.collection_id, built_through, limit + 1))]
    batch, more = rows[:limit], len(rows) > limit
    if batch:
        _replace(conn, field_key, field, kind, batch)
    conn.execute("UPDATE field_span_state SET built_through=?,ready=? WHERE field_key=?",
                 (batch[-1] if batch else built_through, 0 if more else 1, field_key))
    return not more


def due(conn, collection_id: str) -> bool:
    return conn.execute("SELECT 1 FROM field_span_state WHERE collection_id=? AND ready=0 LIMIT 1",
                        (collection_id,)).fetchone() is not None


def listing(conn, collection_id: str, readable: Mapping[str, Any]) -> list[dict[str, str]]:
    """Each index of the collection on a field in `readable`, with its state, for `inspect`."""
    return [{"field": field, "state": "ready" if is_ready else "building"}
            for field, is_ready in conn.execute(
                "SELECT field_path,ready FROM field_span_state WHERE collection_id=? ORDER BY field_path",
                (collection_id,)) if field in readable]


def candidates(conn, field_key: int, *, lo: int | None, hi: int | None, changes: bool,
               narrowed: tuple[str, list] | None) -> list[tuple[int, int, int, int | None, int, str]]:
    """(start_at, start_seq, row_id, end_at, tag, key) of the entries in a window, in one statement.

    `narrowed` is an SQL condition on (value_tag, value_key) and its parameters, a superset of the spans a
    value predicate keeps; the caller applies the predicate itself.
    """
    where, parameters = ["field_key=?"], [field_key]
    if changes:
        if lo is not None:
            where.append("start_at>=?")
            parameters.append(lo)
    elif lo is not None:
        where.append("(end_at IS NULL OR end_at>? OR start_at>=?)")
        parameters += [lo, lo]
    if hi is not None:
        where.append("start_at<?")
        parameters.append(hi)
    if narrowed is not None:
        where.append(narrowed[0])
        parameters += narrowed[1]
    return conn.execute(
        "SELECT start_at,start_seq,row_id,end_at,value_tag,value_key FROM field_spans "  # noqa: S608 - fixed text
        f"WHERE {' AND '.join(where)}", parameters).fetchall()
