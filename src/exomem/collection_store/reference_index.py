"""The declared-reference inverse index: raw values, match keys and warm state (schema 17).

``item_references`` is maintained in the canonical item transaction and built by a
backfill: it derives entirely from items, so a replica leaves it empty and an absent or
``ready=0`` state row means "warming" — inverse reads never report an empty index as
complete.

It holds two kinds of field. A reference field names rows of another collection, and its state row
names that collection. A ``link`` field names pages and rows by canonical ref, and its state row
names no collection (``collections.LINK_TARGET``); `link_match` spells its entries, and a row that
also holds values no ref spells carries one tally entry for them (`UNINDEXED`), which `link_counts`
sums.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from types import SimpleNamespace
from typing import Any

from .. import memory_refs
from .. import structured_collections as collections
from ..planning_values import PlanningValues
from . import typed_storage

# The bounded inbound report's declaration cap; the egress validator shares it.
INBOUND_LIMIT = 64
# A link entry ``unindexed:<n>`` says that its row holds n values that no ref spells. No `link_match`
# spelling starts so, and every citation read asks for an exact ref spelling, so no read finds a tally.
UNINDEXED = "unindexed:"


def _present(conn) -> bool:
    """Whether the store carries schema 17's tables; an older store skips index maintenance."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='item_reference_state'").fetchone() is not None


def _state(conn, collection_id: str, field_path: str):
    return conn.execute(
        "SELECT target_id,built_through,ready FROM item_reference_state "
        "WHERE collection_id=? AND field_path=?", (collection_id, field_path),
    ).fetchone()


_target_cache: OrderedDict = OrderedDict()
_TARGET_CACHE = 64


def _target_schema(conn, target_id: str):
    """(semantic_profile, schema) of the target, cached per collection and manifest hash."""
    row = conn.execute(
        "SELECT m.manifest_hash, m.manifest_text, m.schema_json FROM collections c "
        "JOIN collection_manifests m ON m.collection_id=c.collection_id "
        "AND m.manifest_version=c.manifest_version WHERE c.collection_id=?",
        (target_id,),
    ).fetchone()
    if row is None:
        return None
    key = (target_id, row[0])
    cached = _target_cache.get(key)
    if cached is not None:
        _target_cache.move_to_end(key)
        return cached
    from .. import vault

    data, _, _ = vault.parse_frontmatter(row[1], strict=True)
    target = (data["semantic_profile"],
              collections._parse_schema(data["schema_version"], json.loads(row[2])))
    if len(_target_cache) >= _TARGET_CACHE:
        _target_cache.popitem(last=False)
    _target_cache[key] = target
    return target


def match(target, target_id: str, value: str) -> str | None:
    """What a target row is found by for one stored value, or NULL when it can name no row.

    A canonical row ref matches on ``ref:<item_key>`` only when its profile and collection
    are the target's, as ``BoundReference._lookup`` decides. Every other value matches on
    the serialized natural key the forward path writes into ``items.natural_key``, or
    NULL when the target's key is not a single field whose type this value can be.
    """
    profile, schema = target
    ref = collections.parse_row_ref(value)
    if ref is not None:
        return f"ref:{ref[2]}" if ref[:2] == (profile, target_id) else None
    key = schema.natural_key
    if len(key) != 1:
        return None
    spec = schema.fields.get(key[0])
    if spec is None or spec.type not in collections.REFERENCE_KEY_TYPES:
        return None
    try:
        return collections.manifest_natural_key(
            SimpleNamespace(schema=schema), {key[0]: value})
    except (ValueError, collections.CollectionError):
        return None


# The spelling of a page's entry, which `link_match` writes and `link_page` reads back.
_MEMORY = "memory:"


def link_match(value: str) -> str | None:
    """What a canonical page or row ref is found by, or NULL for any other link value.

    The spellings are ``memory:<uuid>``, ``record:<collection>/<key>`` and ``plan:<collection>/<key>``.
    A title or a path is not indexed: the page it names can change with every rename, and the index
    must rebuild from the stored rows alone.

    These spellings share the ``match`` column with a reference field's ``ref:<key>`` and its natural
    keys, and a natural key may read ``memory:<uuid>``. They never mix, because a link entry's
    ``(collection_id, field_path)`` holds a state row with no target collection and a reference
    entry's holds one that names its target. Every read of link entries joins that state row.
    """
    memory = memory_refs.parse_memory_ref(value)
    if memory is not None:
        return f"{_MEMORY}{memory}"
    row = collections.parse_row_ref(value)
    if row is not None:
        # The two row profiles are fixed native identity protocols (`parse_row_ref`), not vocabulary.
        prefix = "plan" if row[0] == "planning" else "record"
        return f"{prefix}:{row[1]}/{row[2]}"
    return None


def link_page(match: str) -> str | None:
    """The identity of the page that a link entry names, or None for a row's entry or any other entry."""
    return match.removeprefix(_MEMORY) if match.startswith(_MEMORY) else None


def _entries(declaration, target_id: str, target, values: Mapping[str, Any]) -> list[str]:
    """The sorted, distinct match keys that one row's values give a field, a link field's tally included."""
    found = set()
    unindexed = 0
    for value in declaration.values(values):
        if isinstance(declaration, collections.LinkDeclaration):
            key = link_match(value)
            unindexed += key is None
        else:
            key = match(target, target_id, value) if target is not None else None
        if key is not None:
            found.add(key)
    if unindexed:
        found.add(f"{UNINDEXED}{unindexed}")
    return sorted(found)


def link_counts(conn, collection_id: str, fields) -> dict[str, tuple[int, int]]:
    """``(indexed, not_indexed)`` of each named link field, summed from its entries in one statement.

    ``indexed`` counts a row's distinct refs once each; ``not_indexed`` counts every value that no ref
    spells. A field without entries is absent. The counts describe every row, so they answer a caller
    only when its rows release alike.
    """
    # A field's tallies share one prefix, so they sort together and one range seek reads them.
    tallies = ("FROM item_references r WHERE r.collection_id=:cid AND r.field_path=f.value"
               " AND r.match>=:low AND r.match<:high")
    found: dict[str, tuple[int, int]] = {}
    for field, entries, tallied, not_indexed in conn.execute(
            "SELECT f.value, (SELECT count(*) FROM item_references r WHERE r.collection_id=:cid"
            f" AND r.field_path=f.value), (SELECT count(*) {tallies}),"
            f" (SELECT coalesce(sum(CAST(substr(r.match, :skip) AS INTEGER)), 0) {tallies})"
            " FROM json_each(:fields) f",
            {"cid": collection_id, "fields": json.dumps(sorted(fields)), "skip": len(UNINDEXED) + 1,
             "low": UNINDEXED, "high": UNINDEXED[:-1] + chr(ord(UNINDEXED[-1]) + 1)}).fetchall():
        if entries:
            found[field] = (entries - tallied, not_indexed)
    return found


def maintain(conn, manifest, row_id: int, values: Mapping[str, Any]) -> None:
    """Rewrite one source row's index entries inside the canonical item transaction."""
    maintain_many(conn, manifest, ((row_id, values),))


def maintain_many(conn, manifest, rows) -> None:
    """Rewrite the index entries of every ``(row_id, values)`` pair, one pass per declaration.

    The declarations and their state rows are read once per batch, so a collection
    without references pays nothing. Each row's entries are deleted unconditionally, so
    deletes and value rewrites stay in step. New entries are written only when the
    declaration's state row is ready or already past the row — an unbackfilled row is
    the backfill's job, not this one's.
    """
    declarations = collections.indexed_fields(manifest.schema, derived=manifest.derived)
    if not declarations or not _present(conn):
        return
    rows = [(row_id, values) for row_id, values in rows]
    if not rows:
        return
    for declaration in declarations:
        state = _state(conn, manifest.collection_id, declaration.dotted)
        target_schema = (_target_schema(conn, state[0])
                         if state is not None and state[0] != collections.LINK_TARGET else None)
        for row_id, values in rows:
            conn.execute(
                "DELETE FROM item_references WHERE row_id=? AND field_path=?",
                (row_id, declaration.dotted),
            )
            if state is None or (not state[2] and state[1] < row_id):
                continue
            if target_schema is None and state[0] != collections.LINK_TARGET:
                continue
            for found in _entries(declaration, state[0], target_schema, values):
                conn.execute(
                    "INSERT INTO item_references(collection_id, field_path, match, row_id)"
                    " VALUES(?,?,?,?)",
                    (manifest.collection_id, declaration.dotted, found, row_id),
                )


def forget(conn, row_id: int) -> None:
    """Drop every index entry of a retracted row, so no reference lookup finds it."""
    if _present(conn):
        conn.execute("DELETE FROM item_references WHERE row_id=?", (row_id,))


def revise(conn, current, proposed) -> None:
    """Keep state rows in step with a manifest change: removed, added and retargeted fields.

    ``current`` is None for a create. A field is retargeted when its reference names another
    collection, or when it changes between a reference and a link.
    """
    if not _present(conn):
        return
    collection_id = proposed.collection_id
    old = {} if current is None else {
        d.dotted: d for d in collections.indexed_fields(current.schema, derived=current.derived)}
    new = {d.dotted: d for d in collections.indexed_fields(proposed.schema, derived=proposed.derived)}
    for field_path in sorted(set(old) - set(new)):
        conn.execute(
            "DELETE FROM item_reference_state WHERE collection_id=? AND field_path=?",
            (collection_id, field_path))
        conn.execute(
            "DELETE FROM item_references WHERE collection_id=? AND field_path=?",
            (collection_id, field_path))
    for field_path in sorted(set(new) - set(old)):
        conn.execute(
            "INSERT OR REPLACE INTO item_reference_state(collection_id, field_path, target_id,"
            " built_through, ready) VALUES(?,?,?,0,0)",
            (collection_id, field_path, new[field_path].collection))
    for field_path in sorted(set(old) & set(new)):
        if new[field_path].collection == old[field_path].collection:
            continue
        conn.execute(
            "UPDATE item_reference_state SET target_id=?, built_through=0, ready=0 "
            "WHERE collection_id=? AND field_path=?",
            (new[field_path].collection, collection_id, field_path))
        conn.execute(
            "DELETE FROM item_references WHERE collection_id=? AND field_path=?",
            (collection_id, field_path))


def rebuild(conn, collection_id: str) -> None:
    """Reset the collection's index to warming so the backfill rebuilds it from items."""
    if not _present(conn):
        return
    conn.execute(
        "UPDATE item_reference_state SET built_through=0, ready=0 WHERE collection_id=?",
        (collection_id,))
    conn.execute("DELETE FROM item_references WHERE collection_id=?", (collection_id,))


def backfill(conn, manifest, *, limit: int = 128) -> bool:
    """Advance one warming declaration's index by up to ``limit`` rows past ``built_through``.

    Returns True when the batch ran past the last row and the declaration is now ready.
    """
    if not _present(conn):
        return False
    declarations = collections.indexed_fields(manifest.schema, derived=manifest.derived)
    pending = []
    for declaration in declarations:
        state = _state(conn, manifest.collection_id, declaration.dotted)
        if state is None:
            # A restored replica holds no state rows: an absent row warms and the backfill builds it.
            conn.execute(
                "INSERT INTO item_reference_state(collection_id, field_path, target_id,"
                " built_through, ready) VALUES(?,?,?,0,0)",
                (manifest.collection_id, declaration.dotted, declaration.collection))
            state = (declaration.collection, 0, 0)
        if not state[2]:
            pending.append((declaration, state))
    if not pending:
        return False
    declaration, state = min(pending, key=lambda item: item[1][1])
    target_schema = _target_schema(conn, state[0]) if state[0] != collections.LINK_TARGET else None
    rows = typed_storage.hydrate(conn, [
        {"row_id": row_id, "item_key": key, "values_json": raw, "encoding": encoding,
         "collection_id": manifest.collection_id}
        for row_id, key, raw, encoding in conn.execute(
            "SELECT row_id,item_key,values_json,encoding FROM items WHERE collection_id=? AND row_id>? "
            "ORDER BY row_id LIMIT ?", (manifest.collection_id, state[1], limit + 1),
        ).fetchall()])
    batch, more = rows[:limit], len(rows) > limit
    for row in batch:
        row_id = row["row_id"]
        values = json.loads(row["values_json"])
        conn.execute(
            "DELETE FROM item_references WHERE row_id=? AND field_path=?",
            (row_id, declaration.dotted))
        for found in _entries(declaration, state[0], target_schema, values):
            conn.execute(
                "INSERT INTO item_references(collection_id, field_path, match, row_id)"
                " VALUES(?,?,?,?)",
                (manifest.collection_id, declaration.dotted, found, row_id),
            )
    conn.execute(
        "UPDATE item_reference_state SET built_through=?, ready=? "
        "WHERE collection_id=? AND field_path=?",
        (batch[-1]["row_id"] if batch else state[1], 0 if more else 1,
         manifest.collection_id, declaration.dotted))
    return not more


def unseeded(conn) -> list[tuple[str, str, str]]:
    """``(collection_id, field_path, target_id)`` of each current declaration with no state row.

    A plain read, so the serving loop checks out a writer only when there is something to seed.
    """
    if not _present(conn):
        return []
    found = []
    for collection_id, text, schema_json in conn.execute(
            "SELECT m.collection_id, m.manifest_text, m.schema_json FROM collection_manifests m "
            "JOIN collections c ON c.collection_id=m.collection_id "
            "AND c.manifest_version=m.manifest_version").fetchall():
        data, _, _ = _vault().parse_frontmatter(text, strict=True)
        if "schema_version" not in data:
            continue
        item_schema = collections._parse_schema(data["schema_version"], json.loads(schema_json))
        # `derived` is the manifest's own flag, which the stored frontmatter holds as a plain boolean.
        for declaration in collections.indexed_fields(item_schema, derived=data.get("derived") is True):
            if _state(conn, collection_id, declaration.dotted) is None:
                found.append((collection_id, declaration.dotted, declaration.collection))
    return found


def seed_missing(conn) -> None:
    """Seed a ``ready=0`` state row for every declaration of every current manifest that has none.

    A store assembled without the canonical create path — a restore, an import — gets
    its missing state rows here, so its declarations always warm and build.
    """
    for collection_id, field_path, target_id in unseeded(conn):
        conn.execute(
            "INSERT OR IGNORE INTO item_reference_state(collection_id, field_path, target_id,"
            " built_through, ready) VALUES(?,?,?,0,0)",
            (collection_id, field_path, target_id))


def _vault():
    from .. import vault
    return vault


_link_cache: OrderedDict = OrderedDict()
_LINK_CACHE = 256
# Request threads share the cache; an OrderedDict reordered and trimmed from two threads at once can raise.
_link_cache_lock = threading.Lock()


def link_fields(conn) -> dict[str, tuple]:
    """The link declarations of every current manifest, by collection; a collection with none is absent.

    A derived collection holds no link entries (`collections.indexed_fields`), so it is absent too. The
    parse is cached per collection and manifest hash, so a read pays a parse only for a changed manifest.
    """
    found = {}
    for collection_id, digest in conn.execute(
            "SELECT c.collection_id, m.manifest_hash FROM collections c JOIN collection_manifests m "
            "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version").fetchall():
        key = (collection_id, digest)
        with _link_cache_lock:
            links = _link_cache.get(key)
            if links is not None:
                _link_cache.move_to_end(key)
        if links is None:
            text, schema_json = conn.execute(
                "SELECT m.manifest_text, m.schema_json FROM collections c JOIN collection_manifests m "
                "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version "
                "WHERE c.collection_id=?", (collection_id,)).fetchone()
            data, _, _ = _vault().parse_frontmatter(text, strict=True)
            links = ()
            if "schema_version" in data:
                schema = collections._parse_schema(data["schema_version"], json.loads(schema_json))
                links = tuple(d for d in collections.indexed_fields(schema, derived=data.get("derived") is True)
                              if isinstance(d, collections.LinkDeclaration))
            with _link_cache_lock:
                if len(_link_cache) >= _LINK_CACHE:
                    _link_cache.popitem(last=False)
                _link_cache[key] = links
        if links:
            found[collection_id] = links
    return found


def link_pairs(declared: Mapping[str, tuple]) -> str:
    """The declared link fields as the JSON array of `[collection_id, field]` pairs that a statement joins."""
    return json.dumps([[collection_id, d.dotted] for collection_id, fields in declared.items() for d in fields])


def link_pending(conn, declared: Mapping[str, tuple]) -> dict[str, set[str]]:
    """The declared link fields whose index is still building, by collection; a field with no state row is.

    A collection whose fields are all built costs one statement and is absent from the answer.
    """
    pending: dict[str, set[str]] = {}
    for collection_id, field in conn.execute(
            "SELECT json_extract(w.value,'$[0]'), json_extract(w.value,'$[1]') FROM json_each(?) w WHERE NOT EXISTS ("
            "SELECT 1 FROM item_reference_state s WHERE s.collection_id=json_extract(w.value,'$[0]')"
            " AND s.field_path=json_extract(w.value,'$[1]') AND s.target_id=? AND s.ready=1)",
            (link_pairs(declared), collections.LINK_TARGET)).fetchall():
        pending.setdefault(collection_id, set()).add(field)
    return pending


def cited_fields(conn, declared: Mapping[str, tuple], keys: Iterable[str]) -> dict[str, set[str]]:
    """The declared link fields whose index holds one of `keys` (`link_match` spellings), by collection.

    One probe of the index's leading columns, so the cost follows the declared fields, never the rows.
    """
    found: dict[str, set[str]] = {}
    for collection_id, field in conn.execute(
            "SELECT DISTINCT json_extract(w.value,'$[0]'), json_extract(w.value,'$[1]') FROM json_each(?) w"
            " CROSS JOIN json_each(?) a WHERE EXISTS (SELECT 1 FROM item_references r"
            " WHERE r.collection_id=json_extract(w.value,'$[0]') AND r.field_path=json_extract(w.value,'$[1]')"
            " AND r.match=a.value)", (link_pairs(declared), json.dumps(sorted(keys)))).fetchall():
        found.setdefault(collection_id, set()).add(field)
    return found


def link_entries(conn, row_ids: Iterable[int]) -> dict[int, list[str]]:
    """The link entries (`link_match` spellings) that the index holds for each of `row_ids`; a row with none is absent.

    A reference field's entries share the table and may spell a natural key as a page, so only a field whose
    state row names no target counts.
    """
    found: dict[int, list[str]] = {}
    for row_id, match in conn.execute(
            "SELECT r.row_id, r.match FROM item_references r JOIN item_reference_state s "
            "ON s.collection_id=r.collection_id AND s.field_path=r.field_path AND s.target_id=? "
            "WHERE r.row_id IN (SELECT value FROM json_each(?))",
            (collections.LINK_TARGET, json.dumps(sorted(set(row_ids))))):
        found.setdefault(row_id, []).append(match)
    return found


def unbuilt_rows(conn, row_ids: Iterable[int]) -> set[int]:
    """The rows of `row_ids` whose link entries the backfill has not built yet, so no read can see their links.

    A write keeps no entry for a row past a warming field's `built_through`; the backfill builds it later.
    """
    return {row_id for (row_id,) in conn.execute(
        "SELECT i.row_id FROM items i JOIN item_reference_state s ON s.collection_id=i.collection_id "
        "AND s.target_id=? AND s.ready=0 AND s.built_through<i.row_id "
        "WHERE i.row_id IN (SELECT value FROM json_each(?))",
        (collections.LINK_TARGET, json.dumps(sorted(set(row_ids)))))}


def due(conn) -> tuple[str, ...]:
    """Collections whose reference index is warming, for the serving backfill loop."""
    return tuple(cid for (cid,) in conn.execute(
        "SELECT DISTINCT collection_id FROM item_reference_state WHERE ready=0").fetchall())


def inbound(operation, conn, manifest) -> list[dict]:
    """The declarations of other collections that name this one, as inspection's ``inbound``.

    A source the caller cannot read, or whose declared member path is withheld, is
    omitted entirely — the list never names a hidden collection.
    """
    if not _present(conn):
        return {"declarations": [], "truncated": False, "total": 0}
    entries = []
    # Every inverse query over this collection is refused when its natural key is withheld.
    plan = operation.field_plan(manifest)
    if plan.natural_key_withheld or not plan.manifest.schema.natural_key:
        return {"declarations": [], "truncated": False, "total": 0}
    rows = conn.execute(
        "SELECT c.collection_id, c.manifest_path, m.manifest_text, m.schema_json FROM collections c "
        "JOIN collection_manifests m ON m.collection_id=c.collection_id "
        "AND m.manifest_version=c.manifest_version").fetchall()
    from .. import vault

    for source_id, path, text, schema_json in rows:
        data, _, _ = vault.parse_frontmatter(text, strict=True)
        schema = collections._parse_schema(data["schema_version"], json.loads(schema_json))
        targeting = [d for d in collections.declared_references(schema)
                     if d.collection == manifest.collection_id]
        if not targeting:
            continue
        try:
            operation.require_collection(source_id)
        except collections.CollectionError:  # a source the caller cannot read is omitted, never named
            continue
        source_manifest = collections.parse_manifest_bytes(
            operation.root, path, text.encode(),
            planning_values=PlanningValues(operation.root, server_side=True))
        plan = operation.field_plan(source_manifest)
        visible_schema = plan.manifest.schema
        for declaration in targeting:
            visible = next((d for d in collections.declared_references(visible_schema)
                            if d.dotted == declaration.dotted), None)
            if visible is None or not plan.admits_member_path(declaration.path):
                continue
            state = conn.execute(
                "SELECT ready FROM item_reference_state WHERE collection_id=? AND field_path=?",
                (source_id, declaration.dotted)).fetchone()
            entries.append({"collection": source_id, "field": declaration.dotted,
                            "state": "ready" if state is not None and state[0] else "warming"})
    entries.sort(key=lambda entry: (entry["collection"], entry["field"]))
    return {"declarations": entries[:INBOUND_LIMIT],
            "truncated": len(entries) > INBOUND_LIMIT,
            "total": len(entries)}
