"""The declared-reference inverse index: raw values, match keys and warm state (schema 17).

``item_references`` is maintained in the canonical item transaction and built by a
backfill: it derives entirely from items, so a replica leaves it empty and an absent or
``ready=0`` state row means "warming" — inverse reads never report an empty index as
complete.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from .. import structured_collections as collections
from ..planning_values import PlanningValues
from . import typed_storage


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
    declarations = collections.declared_references(manifest.schema)
    if not declarations or not _present(conn):
        return
    rows = [(row_id, values) for row_id, values in rows]
    if not rows:
        return
    for declaration in declarations:
        state = _state(conn, manifest.collection_id, declaration.dotted)
        target_schema = _target_schema(conn, state[0]) if state is not None else None
        for row_id, values in rows:
            conn.execute(
                "DELETE FROM item_references WHERE row_id=? AND field_path=?",
                (row_id, declaration.dotted),
            )
            if state is None or (not state[2] and state[1] < row_id):
                continue
            if target_schema is None:
                continue
            matches = set()
            for value in declaration.values(values):
                found = match(target_schema, state[0], value)
                if found is not None:
                    matches.add(found)
            for found in sorted(matches):
                conn.execute(
                    "INSERT INTO item_references(collection_id, field_path, match, row_id)"
                    " VALUES(?,?,?,?)",
                    (manifest.collection_id, declaration.dotted, found, row_id),
                )


def revise(conn, collection_id: str, *, dropped: tuple[str, ...],
           added: tuple, retargeted: tuple[str, ...]) -> None:
    """Keep state rows in step with a revise: removed, added and retargeted declarations."""
    if not _present(conn):
        return
    for field_path in dropped:
        conn.execute(
            "DELETE FROM item_reference_state WHERE collection_id=? AND field_path=?",
            (collection_id, field_path))
        conn.execute(
            "DELETE FROM item_references WHERE collection_id=? AND field_path=?",
            (collection_id, field_path))
    for declaration in added:
        conn.execute(
            "INSERT OR REPLACE INTO item_reference_state(collection_id, field_path, target_id,"
            " built_through, ready) VALUES(?,?,?,0,0)",
            (collection_id, declaration.dotted, declaration.collection))
    for declaration in retargeted:
        conn.execute(
            "UPDATE item_reference_state SET target_id=?, built_through=0, ready=0 "
            "WHERE collection_id=? AND field_path=?",
            (declaration.collection, collection_id, declaration.dotted))
        conn.execute(
            "DELETE FROM item_references WHERE collection_id=? AND field_path=?",
            (collection_id, declaration.dotted))


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
    declarations = collections.declared_references(manifest.schema)
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
    target_schema = _target_schema(conn, state[0])
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
        if target_schema is not None:
            matches = set()
            for value in declaration.values(values):
                found = match(target_schema, state[0], value)
                if found is not None:
                    matches.add(found)
            for found in sorted(matches):
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


def seed_missing(conn) -> None:
    """Seed a ``ready=0`` state row for every declaration of every current manifest that has none.

    A store assembled without the canonical create path — a restore, an import — runs
    this once per writer handle so its declarations always warm and build.
    """
    if not _present(conn):
        return
    for collection_id, text, schema_json in conn.execute(
            "SELECT m.collection_id, m.manifest_text, m.schema_json FROM collection_manifests m "
            "JOIN collections c ON c.collection_id=m.collection_id "
            "AND c.manifest_version=m.manifest_version").fetchall():
        data, _, _ = _vault().parse_frontmatter(text, strict=True)
        if "schema_version" not in data:
            continue
        item_schema = collections._parse_schema(data["schema_version"], json.loads(schema_json))
        for declaration in collections.declared_references(item_schema):
            conn.execute(
                "INSERT OR IGNORE INTO item_reference_state(collection_id, field_path, target_id,"
                " built_through, ready) VALUES(?,?,?,0,0)",
                (collection_id, declaration.dotted, declaration.collection))


def _vault():
    from .. import vault
    return vault


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
    return {"declarations": entries[:64],
            "truncated": len(entries) > 64,
            "total": len(entries)}
