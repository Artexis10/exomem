"""Declared exact rollups: versioned definitions, transactional bucket upkeep and backfill.

OpenSpec add-collection-query-engine §13. A manifest's ``rollups`` mapping
names day or ISO-week buckets over a date or datetime field, at most four
group dimensions and the count/sum/avg/min/max/latest reductions kept per
value field. The source-local day basis (§3) is the timestamp field's own
declaration: a datetime field may name its per-record ``offset`` field.
Definitions are migration-built: an empty collection's rollup is ready at
once; a populated one stays ``building`` until bounded writer-owned backfill
has covered every row. Every canonical item write updates the
affected old and new buckets in its own transaction, and replacing a held
min/max/latest member recomputes that bucket from its indexed members, so a
ready rollup is never stale. Release is not decided here: the query planner
reads a rollup only under proven uniform release.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from .. import structured_collections as collections
from ..query_engine.buckets import (
    EXTREMES,
    NUMERIC,
    REDUCTIONS,
    Accumulator,
    Basis,
    bucket_key,
    declared_kind,
    group_key,
)
from . import typed_storage

#: Each item mutation updates at most an old and a new bucket per rollup, so
#: eight rollups bound a write's fan-out at sixteen bucket updates.
MAX_ROLLUPS = 8
MAX_GROUP_DIMENSIONS = 4
MAX_VALUE_FIELDS = 8
_KEYS = frozenset({"bucket", "timestamp", "group_by", "values"})
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SCALARS = frozenset({"string", "integer", "number", "boolean", "date", "datetime", "enum", "link"})
_FLAGGED = object()


@dataclass(frozen=True, slots=True)
class Rollup:
    name: str
    bucket: str
    basis: Basis
    groups: tuple[str, ...]
    values: tuple[tuple[str, tuple[str, ...]], ...]
    kinds: tuple[str, ...]

    @property
    def members(self) -> bool:
        return any(op in EXTREMES for _, ops in self.values for op in ops)

    def encoded(self) -> str:
        return json.dumps({"version": 1, "bucket": self.bucket, "basis": self.basis.describe(),
                           "group_by": list(self.groups), "values": {name: list(ops) for name, ops in self.values},
                           "kinds": list(self.kinds)}, sort_keys=True, separators=(",", ":"))

    def reduces(self, field: str, op: str) -> bool:
        return any(name == field and op in ops for name, ops in self.values)


@functools.lru_cache(maxsize=256)
def decode(name: str, encoded: str) -> Rollup:
    raw = json.loads(encoded)
    if raw.get("version") != 1:
        raise ValueError("unsupported rollup definition")
    basis = raw["basis"]
    return Rollup(name, raw["bucket"], Basis(basis["field"], basis["kind"], basis["offset"]),
                  tuple(raw["group_by"]), tuple((field, tuple(ops)) for field, ops in raw["values"].items()),
                  tuple(raw["kinds"]))


def _refuse(code: str, reason: str, at: str):
    raise collections.CollectionError(code, reason, details={"at": at})


def basis(fields: Mapping, name: str) -> Basis | None:
    """The declared source-local day basis of a date or datetime field, else None."""
    spec = fields.get(name)
    if spec is None or spec.type not in {"date", "datetime"}:
        return None
    return Basis(name, "date", None) if spec.type == "date" else Basis(name, "instant", spec.offset)


def normalize(fields: Mapping, raw) -> tuple[Rollup, ...]:
    """Validate rollup declarations against the collection's declared fields, before any write."""
    if raw is None:
        return ()
    invalid = "INVALID_ROLLUP_DECLARATION"
    if not isinstance(raw, Mapping):
        _refuse(invalid, "rollups must map names to declarations", "rollups")
    if len(raw) > MAX_ROLLUPS:
        _refuse("ROLLUP_LIMIT", f"at most {MAX_ROLLUPS} rollups per collection, so one write updates "
                f"at most {2 * MAX_ROLLUPS} buckets", "rollups")
    kinds = {name: declared_kind(spec.type, spec.enum) for name, spec in fields.items()}
    rollups = []
    for name, declaration in raw.items():
        at = f"rollups.{name}"
        if type(name) is not str or not _NAME.fullmatch(name) or not isinstance(declaration, Mapping):
            _refuse(invalid, "a rollup is a named mapping", at)
        if "partitions" in declaration:
            _refuse("ROLLUP_PARTITIONS_UNSUPPORTED", "maintained release partitions wait for measured "
                    "demand; mixed release uses an exact base reduction", f"{at}.partitions")
        if set(declaration) - _KEYS:
            _refuse(invalid, f"unknown rollup keys {sorted(map(str, set(declaration) - _KEYS))}", at)
        bucket = declaration.get("bucket")
        if bucket not in {"day", "week"}:
            _refuse(invalid, "bucket is day or week", f"{at}.bucket")
        timestamp = declaration.get("timestamp")
        day_basis = basis(fields, timestamp) if type(timestamp) is str else None
        if day_basis is None:
            _refuse(invalid, "timestamp is a declared date or datetime field", f"{at}.timestamp")
        groups = declaration.get("group_by", [])
        if not isinstance(groups, list):
            _refuse(invalid, "group_by is a list of fields", f"{at}.group_by")
        if len(groups) > MAX_GROUP_DIMENSIONS:
            _refuse("ROLLUP_LIMIT", f"at most {MAX_GROUP_DIMENSIONS} group dimensions", f"{at}.group_by")
        if (len(set(map(str, groups))) != len(groups) or timestamp in groups
                or any(kinds.get(group) not in _SCALARS for group in groups)):
            _refuse(invalid, "group dimensions are distinct declared scalar fields", f"{at}.group_by")
        values = declaration.get("values", {})
        if not isinstance(values, Mapping):
            _refuse(invalid, "values map fields to reductions", f"{at}.values")
        if len(values) > MAX_VALUE_FIELDS:
            _refuse("ROLLUP_LIMIT", f"at most {MAX_VALUE_FIELDS} value fields", f"{at}.values")
        reduced = []
        for field, ops in values.items():
            kind = kinds.get(field)
            if kind not in _SCALARS or not isinstance(ops, list) or not ops or len(set(map(str, ops))) != len(ops):
                _refuse(invalid, "each value field is declared and lists distinct reductions", f"{at}.values.{field}")
            unsupported = [op for op in ops if op not in REDUCTIONS]
            if unsupported:
                _refuse(invalid, f"rollups keep only {', '.join(REDUCTIONS)}; percentile and distinct "
                        "count need an exact base reduction", f"{at}.values.{field}")
            if ({"sum", "avg"} & set(ops) and kind not in NUMERIC) or ({"min", "max"} & set(ops) and kind == "boolean"):
                _refuse(invalid, "sum/avg need a numeric field; min/max an ordered one", f"{at}.values.{field}")
            reduced.append((field, tuple(op for op in REDUCTIONS if op in ops)))
        rollups.append(Rollup(name, bucket, day_basis, tuple(groups), tuple(reduced),
                              tuple(kinds[field] for field, _ in reduced)))
    return tuple(rollups)


def _drop(conn, rollup_id: int) -> None:
    for table in ("rollup_members", "rollup_buckets", "rollup_definitions"):
        conn.execute(f"DELETE FROM {table} WHERE rollup_id=?", (rollup_id,))  # noqa: S608 - fixed names


def prepare(conn, manifest, data: Mapping) -> None:
    """Resolve rollup intent at governed create/revise: keep unchanged ones, rebuild changed ones."""
    declared = {rollup.name: rollup for rollup in normalize(manifest.schema.fields, data.get("rollups"))}
    cid = manifest.collection_id
    current = {name: (rollup_id, encoded) for rollup_id, name, encoded in conn.execute(
        "SELECT rollup_id,name,definition_json FROM rollup_definitions WHERE collection_id=?", (cid,)).fetchall()}
    for name, (rollup_id, encoded) in current.items():
        if name not in declared or declared[name].encoded() != encoded:
            _drop(conn, rollup_id)
    empty = conn.execute("SELECT 1 FROM items WHERE collection_id=? LIMIT 1", (cid,)).fetchone() is None
    for name, rollup in declared.items():
        if current.get(name, (None, None))[1] != rollup.encoded():
            conn.execute("INSERT INTO rollup_definitions(collection_id,name,state,definition_json) VALUES(?,?,?,?)",
                         (cid, name, "ready" if empty else "building", rollup.encoded()))


def group_text(values: Mapping, names) -> str:
    """A bucket's group identity: exact stored values, so 1, 1.0 and true stay apart."""
    return json.dumps(group_key(values, names), separators=(",", ":"))


def _place(rollup: Rollup, values: Mapping, key: str):
    located = rollup.basis.locate(values)
    if located is None:
        return _FLAGGED
    day, order = located
    return (bucket_key(day, rollup.bucket), group_text(values, rollup.groups), order,
            tuple(values.get(field) for field, _ in rollup.values))


def _same(old, new) -> bool:
    """Whether a row's contribution is unchanged, telling 1 from 1.0 and 0.0 from -0.0."""
    if old is _FLAGGED or new is _FLAGGED:
        return old is new
    return old[:3] == new[:3] and json.dumps(old[3]) == json.dumps(new[3])


def _state(conn, rollup_id, bucket, groups, rollup):
    row = conn.execute("SELECT state_json FROM rollup_buckets WHERE rollup_id=? AND bucket=? AND groups=?",
                       (rollup_id, bucket, groups)).fetchone()
    if row is None:
        return 0, [Accumulator() for _ in rollup.values]
    stored = json.loads(row[0])
    return stored["rows"], [Accumulator(state) for state in stored["fields"]]


def _save(conn, rollup_id, bucket, groups, rows, fields) -> None:
    if not rows:
        conn.execute("DELETE FROM rollup_buckets WHERE rollup_id=? AND bucket=? AND groups=?",
                     (rollup_id, bucket, groups))
        return
    state = json.dumps({"rows": rows, "fields": [field.state() for field in fields]}, separators=(",", ":"))
    conn.execute("INSERT INTO rollup_buckets VALUES(?,?,?,?) ON CONFLICT(rollup_id,bucket,groups) "
                 "DO UPDATE SET state_json=excluded.state_json", (rollup_id, bucket, groups, state))


def _add(conn, rollup_id, rollup, place, row_id, key) -> None:
    bucket, groups, order, values = place
    rows, fields = _state(conn, rollup_id, bucket, groups, rollup)
    for field, (_, ops), kind, value in zip(fields, rollup.values, rollup.kinds, values, strict=True):
        field.add(value, kind, key, order, not EXTREMES.isdisjoint(ops))
    _save(conn, rollup_id, bucket, groups, rows + 1, fields)
    if rollup.members:
        conn.execute("INSERT INTO rollup_members VALUES(?,?,?,?)", (rollup_id, bucket, groups, row_id))


def _remove(conn, rollup_id, rollup, place, row_id, key) -> None:
    bucket, groups, order, values = place
    rows, fields = _state(conn, rollup_id, bucket, groups, rollup)
    stale = False
    for field, kind, value in zip(fields, rollup.kinds, values, strict=True):
        stale |= field.remove(value, kind, key, order)
    if rollup.members:
        conn.execute("DELETE FROM rollup_members WHERE rollup_id=? AND bucket=? AND groups=? AND row_id=?",
                     (rollup_id, bucket, groups, row_id))
    if stale and rows > 1:
        # The held extreme left this bucket: recompute it from the remaining indexed members.
        for field in fields:
            field.clear_extremes()
        for member, member_key in conn.execute(
                "SELECT m.row_id,i.item_key FROM rollup_members m JOIN items i ON i.row_id=m.row_id "
                "WHERE m.rollup_id=? AND m.bucket=? AND m.groups=?", (rollup_id, bucket, groups)).fetchall():
            current = _place(rollup, typed_storage.item_values(conn, member), member_key)
            if current is _FLAGGED or current[:2] != (bucket, groups):
                raise RuntimeError("rollup member index disagrees with its bucket")
            for field, (_, ops), kind, value in zip(fields, rollup.values, rollup.kinds, current[3], strict=True):
                if value is not None and not EXTREMES.isdisjoint(ops):
                    field.extreme(value, kind, member_key, current[2])
    _save(conn, rollup_id, bucket, groups, rows - 1, fields)


def _apply(conn, rollup_id, rollup, row_id, key, values, previous) -> int:
    """Move one row's contribution; returns the change in flagged rows."""
    old = None if previous is None else _place(rollup, previous, key)
    new = _place(rollup, values, key)
    if old is not None and _same(old, new):
        return 0
    if old is not None and old is not _FLAGGED:
        _remove(conn, rollup_id, rollup, old, row_id, key)
    if new is not _FLAGGED:
        _add(conn, rollup_id, rollup, new, row_id, key)
    return (new is _FLAGGED) - (old is _FLAGGED)


def maintain(conn, collection_id: str, row_id: int, key: str, values: Mapping, *, previous=None) -> None:
    """Update every declared rollup's old and new buckets inside the canonical item transaction.

    A building rollup takes only rows its backfill already covered; the
    backfill reads later rows' current values when it reaches them.
    """
    for rollup_id, name, state, encoded, last in conn.execute(
            "SELECT rollup_id,name,state,definition_json,last_row_id FROM rollup_definitions "
            "WHERE collection_id=?", (collection_id,)).fetchall():
        if state == "building" and row_id > last:
            continue
        flagged = _apply(conn, rollup_id, decode(name, encoded), row_id, key, values, previous)
        if flagged:
            conn.execute("UPDATE rollup_definitions SET flagged=flagged+? WHERE rollup_id=?", (flagged, rollup_id))


def backfill_batch(conn, collection_id: str, *, limit: int = 128) -> bool:
    """Fold at most ``limit`` further rows into each building rollup; True once all are ready.

    The caller owns the writer lease, transaction and collection authorization.
    """
    if type(limit) is not int or not 0 < limit <= 500:
        raise ValueError("rollup backfill limit must be between 1 and 500")
    building = conn.execute("SELECT rollup_id,name,definition_json,last_row_id FROM rollup_definitions "
                            "WHERE collection_id=? AND state='building'", (collection_id,)).fetchall()
    for rollup_id, name, encoded, last in building:
        rollup = decode(name, encoded)
        rows = typed_storage.hydrate(conn, [
            {"row_id": row_id, "item_key": key, "values_json": values, "encoding": encoding,
             "collection_id": collection_id}
            for row_id, key, values, encoding in conn.execute(
                "SELECT row_id,item_key,values_json,encoding FROM items WHERE collection_id=? AND row_id>? "
                "ORDER BY row_id LIMIT ?", (collection_id, last, limit)).fetchall()])
        flagged = 0
        for row in rows:
            flagged += _apply(conn, rollup_id, rollup, row["row_id"], row["item_key"],
                              json.loads(row["values_json"]), None)
            last = row["row_id"]
        done = conn.execute("SELECT 1 FROM items WHERE collection_id=? AND row_id>? LIMIT 1",
                            (collection_id, last)).fetchone() is None
        conn.execute("UPDATE rollup_definitions SET last_row_id=?,flagged=flagged+?,state=? WHERE rollup_id=?",
                     (last, flagged, "ready" if done else "building", rollup_id))
    return conn.execute("SELECT 1 FROM rollup_definitions WHERE collection_id=? AND state='building' LIMIT 1",
                        (collection_id,)).fetchone() is None


def definitions(conn, collection_id: str) -> tuple[tuple[int, Rollup, str, int], ...]:
    """Each declared rollup with its state and flagged-row count, for the planner."""
    return tuple((rollup_id, decode(name, encoded), state, flagged)
                 for rollup_id, name, state, encoded, flagged in conn.execute(
                     "SELECT rollup_id,name,state,definition_json,flagged FROM rollup_definitions "
                     "WHERE collection_id=? ORDER BY name", (collection_id,)).fetchall())


def bucket_states(conn, rollup_id: int, *, first: str | None = None, last: str | None = None):
    """Stored buckets from ``first`` to ``last`` (inclusive bucket keys) in primary-key order.

    Yields (bucket, group identity text, tagged groups, rows, accumulators,
    stored bytes); the caller stops reading when its page is full.
    """
    cursor = conn.execute(
        "SELECT bucket,groups,state_json FROM rollup_buckets WHERE rollup_id=? AND bucket>=? AND bucket<=? "
        "ORDER BY bucket,groups", (rollup_id, first or "", last or "\uffff"))
    try:
        for bucket, groups, state in cursor:
            stored = json.loads(state)
            yield bucket, groups, tuple(tuple(item) for item in json.loads(groups)), stored["rows"], [
                Accumulator(field) for field in stored["fields"]], len(state) + len(groups)
    finally:
        cursor.close()
