"""Canonical typed-v1 collection encoding (OpenSpec add-collection-query-engine §12).

A collection is either ``json-v1`` (values in ``items.values_json`` and
``item_versions``) or ``typed-v1``. A typed collection stores each declared
field in internal ordinal columns of two collection-local STRICT tables: a
mutable current table and an append-only version table. Field names live only
in the mapping's layout JSON; DDL is built from the collection UUID hex, the
mapping generation and ordinals, never from authored text.

Each field is a discriminator ``t``, an authoritative value ``v`` and a derived
comparison key ``k``:

==  ================  =========================  ===============================
t   meaning           v                          k
==  ================  =========================  ===============================
0   missing           NULL                       NULL
1   null              NULL                       NULL
2   boolean           INTEGER 0/1                NULL
3   signed 64-bit     INTEGER                    NULL
4   larger integer    canonical decimal TEXT     exact number key (scalars.py)
5   finite float      IEEE-754 binary64 BLOB     REAL mirror
6   string            TEXT                       NULL
7   array/object      canonical JSON TEXT        NULL
==  ================  =========================  ===============================

Comparison keys are derived and never decoded or hashed. Decoding restores the
original Python int/float/bool subtype, so the existing payload hash and every
JSON reader see exactly the input json-v1 would have stored. Keys absent from
the layout (fields added by a later revise) live in the bounded residual JSON
object ``r``.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import sqlite3
import struct
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    Table,
    Text,
    and_,
    case,
    func,
    literal,
    or_,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.schema import CreateTable
from sqlalchemy.types import UserDefinedType

from .. import records
from ..query_engine.scalars import scalar_key
from . import tables

JSON_V1 = "json-v1"
TYPED_V1 = "typed-v1"
TYPED_V2 = "typed-v2"
MISSING, NULL, BOOLEAN, INT64, BIGINT, FLOAT, STRING, JSON = range(8)
_INT64 = range(-(2**63), 2**63)
_ABSENT = object()


class TypedStorageError(RuntimeError):
    """Stored typed values are not a valid encoding; never served as data."""

    code = "COLLECTION_TYPED_ENCODING_INVALID"


def canonical_json(value: Any) -> str:
    """The exact text json-v1 stores for ``value`` (sorted keys, compact UTF-8)."""
    return records._canonical_json(value).decode("utf-8")


@dataclass(frozen=True, slots=True)
class Layout:
    collection_id: str
    generation: int
    fields: tuple[str, ...]
    #: The relations a read joins: the store's ``items`` and current table by default, or a
    #: derived collection's one rows table for both (``derived_rows``). Reads only; DDL never uses them.
    items: str = "items"
    current: str = ""

    def __post_init__(self):
        if str(UUID(self.collection_id)) != self.collection_id:
            raise ValueError("canonical collection UUID required")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("positive generation required")
        if len(set(self.fields)) != len(self.fields) or not all(type(name) is str for name in self.fields):
            raise ValueError("layout fields must be distinct strings")

    @property
    def _suffix(self) -> str:
        return f"{UUID(self.collection_id).hex}_{self.generation}"

    @property
    def current_table(self) -> str:
        return f"tc_{self._suffix}"

    @property
    def current_relation(self) -> str:
        return self.current or self.current_table

    @property
    def version_table(self) -> str:
        return f"tv_{self._suffix}"

    @property
    def value_columns(self) -> tuple[str, ...]:
        return tuple(f"{part}{ordinal}" for ordinal in range(len(self.fields)) for part in "tvk")

    @property
    def encoded(self) -> str:
        return canonical_json({"version": 1, "fields": list(self.fields)})

    #: A class constant, not a property: scans read it once per row.
    encoding = TYPED_V1


@functools.lru_cache(maxsize=64)
def _layout(collection_id: str, generation: int, encoded: str) -> Layout:
    raw = json.loads(encoded)
    if not isinstance(raw, dict):
        raise TypedStorageError("unsupported typed layout descriptor")
    if raw.get("version") == 2:
        from .native_layout import parse

        layout = parse(collection_id, generation, raw)
    elif set(raw) == {"version", "fields"} and raw["version"] == 1 and isinstance(raw["fields"], list):
        layout = Layout(collection_id, generation, tuple(raw["fields"]))
    else:
        raise TypedStorageError("unsupported typed layout descriptor")
    if layout.encoded != encoded:
        raise TypedStorageError("typed layout descriptor is not canonical")
    return layout


def encode_value(value: Any) -> tuple[int, Any, Any]:
    if value is _ABSENT:
        return MISSING, None, None
    if value is None:
        return NULL, None, None
    if type(value) is bool:
        return BOOLEAN, int(value), None
    if type(value) is int:
        if value in _INT64:
            return INT64, value, None
        return BIGINT, str(value), scalar_key(value, "number")[1]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("typed-v1 numbers must be finite")
        return FLOAT, struct.pack(">d", value), value
    if type(value) is str:
        return STRING, value, None
    return JSON, canonical_json(value), None


def decode_value(tag: int, value: Any, key: Any) -> Any:
    """Restore the stored Python value; ``key`` is accepted only as shape evidence."""
    if tag in (MISSING, NULL) and value is None:
        return _ABSENT if tag == MISSING else None
    if tag == BOOLEAN and type(value) is int and value in (0, 1):
        return bool(value)
    if tag == INT64 and type(value) is int:
        return value
    if tag == BIGINT and type(value) is str:
        number = int(value)
        if str(number) == value and number not in _INT64:
            return number
    if tag == FLOAT and type(value) is bytes and len(value) == 8:
        return struct.unpack(">d", value)[0]
    if tag == STRING and type(value) is str:
        return value
    if tag == JSON and type(value) is str:
        return json.loads(value)
    raise TypedStorageError("stored typed value does not match its discriminator")


def encode_row(layout: Layout, values: Mapping[str, Any], *, connection=None, scope=None) -> tuple[Any, ...]:
    """Column parameters in ``value_columns`` order, then the residual object.

    ``scope`` is the caller's ``native_metadata.WriteScope`` for the open transaction, if it encodes many rows.
    """
    if layout.encoding == TYPED_V2:
        from . import native_rows

        if connection is None:
            raise TypedStorageError("native row encoding requires its metadata connection")
        return native_rows.encode_row(connection, layout, values, scope=scope)
    columns = []
    for name in layout.fields:
        columns.extend(encode_value(values.get(name, _ABSENT)))
    known = set(layout.fields)
    residual = {name: value for name, value in values.items() if name not in known}
    return (*columns, canonical_json(residual) if residual else None)


def decode_row(layout: Layout, columns, *, connection=None) -> dict[str, Any]:
    if layout.encoding == TYPED_V2:
        from . import native_rows

        if connection is None:
            raise TypedStorageError("native row decoding requires its metadata connection")
        return native_rows.decode_row(connection, layout, columns, database=layout.database)
    columns = tuple(columns)
    if len(columns) != 3 * len(layout.fields) + 1:
        raise TypedStorageError("typed row width differs from its layout")
    values = {}
    for ordinal, name in enumerate(layout.fields):
        value = decode_value(*columns[3 * ordinal:3 * ordinal + 3])
        if value is not _ABSENT:
            values[name] = value
    if columns[-1] is not None:
        residual = json.loads(columns[-1])
        if not isinstance(residual, dict) or not residual or set(residual) & set(values):
            raise TypedStorageError("typed residual object is invalid")
        values.update(residual)
    return dict(sorted(values.items()))


# --- DDL: internal names only -------------------------------------------------


class _AnyValue(UserDefinedType):
    # SQLite STRICT ANY preserves the encoded scalar's exact DBAPI representation.
    cache_ok = True

    def get_col_spec(self, **kw):
        return "ANY"


@functools.lru_cache(maxsize=64)
def _declarations(layout: Layout):
    if layout.encoding == TYPED_V2:
        from . import native_tables

        current = native_tables.current(layout, layout.current_table)
        version = native_tables.history(layout)
        statement = insert(current)
        upsert = statement.on_conflict_do_update(index_elements=[current.c.row_id], set_={
            name: statement.excluded[name] for name in ("item_key", "row_version", *layout.value_columns, "r")})
        return current, version, upsert, version.insert()
    metadata = MetaData()
    items = tables.items.to_metadata(metadata)
    identity = tables.version_identity.to_metadata(metadata)

    def columns():
        fields: list[Any] = []
        for ordinal in range(len(layout.fields)):
            t = Column(f"t{ordinal}", Integer, nullable=False)
            v, k = Column(f"v{ordinal}", _AnyValue), Column(f"k{ordinal}", _AnyValue)
            # The discriminator vocabulary is the typed-v1 wire encoding above.
            fields.extend((t, v, k, CheckConstraint(case(
                (t.in_((MISSING, NULL)), and_(v.is_(None), k.is_(None))),
                (t == BOOLEAN, and_(func.typeof(v) == "integer", v.in_((literal(0), literal(1))), k.is_(None))),
                (t == INT64, and_(func.typeof(v) == "integer", k.is_(None))),
                (t == BIGINT, and_(func.typeof(v) == "text", func.typeof(k) == "text")),
                (t == FLOAT, and_(func.typeof(v) == "blob", func.length(v) == 8, func.typeof(k) == "real")),
                (t == STRING, and_(func.typeof(v) == "text", k.is_(None))),
                (t == JSON, and_(func.typeof(v) == "text", func.json_valid(v), k.is_(None))),
                else_=0,
            ))))
        residual = Column("r", Text)
        fields.extend((residual, CheckConstraint(or_(residual.is_(None), func.json_valid(residual)))))
        return fields

    current = Table(layout.current_table, metadata,
        Column("row_id", Integer, ForeignKey(items.c.row_id), primary_key=True),
        Column("row_version", Integer, nullable=False), *columns(), sqlite_strict=True)
    version = Table(layout.version_table, metadata,
        Column("row_id", Integer, primary_key=True), Column("row_version", Integer, primary_key=True),
        Column("body", Text, nullable=False), *columns(),
        ForeignKeyConstraint(["row_id", "row_version"], [identity.c.row_id, identity.c.row_version],
                             deferrable=True, initially="DEFERRED"),
        sqlite_strict=True, sqlite_with_rowid=False)
    upsert = insert(current)
    upsert = upsert.on_conflict_do_update(index_elements=[current.c.row_id], set_={
        name: upsert.excluded[name] for name in ("row_version", *layout.value_columns, "r")})
    return current, version, upsert, version.insert()


def _version_triggers(layout: Layout) -> tuple[str, ...]:
    table = layout.version_table
    message = f"append-only: {table} rows cannot be changed"
    return (
        f"""CREATE TRIGGER IF NOT EXISTS {table}_append_only_insert BEFORE INSERT ON {table}
        WHEN EXISTS (SELECT 1 FROM {table} WHERE row_id=NEW.row_id AND row_version=NEW.row_version)
        BEGIN SELECT RAISE(ABORT, '{message}'); END""",
        f"""CREATE TRIGGER IF NOT EXISTS {table}_append_only_update BEFORE UPDATE ON {table}
        BEGIN SELECT RAISE(ABORT, '{message}'); END""",
        f"""CREATE TRIGGER IF NOT EXISTS {table}_append_only_delete BEFORE DELETE ON {table}
        BEGIN SELECT RAISE(ABORT, '{message}'); END""",
        f"""CREATE TRIGGER IF NOT EXISTS {table}_typed_rows_only BEFORE INSERT ON {table}
        WHEN NOT EXISTS (SELECT 1 FROM items i JOIN typed_encoding_mappings m ON m.collection_id=i.collection_id
                         WHERE i.row_id=NEW.row_id AND i.collection_id='{layout.collection_id}'
                         AND i.encoding='{layout.encoding}' AND m.generation={layout.generation} AND m.state='ready')
        BEGIN SELECT RAISE(ABORT, 'typed versions belong to the ready typed generation of their collection'); END""",
        # A typed payload precedes its own identity; it never joins an existing
        # (JSON or typed) one. Its deferred foreign key refuses a payload whose
        # typed identity never follows, and JSON identities need JSON payloads.
        f"""CREATE TRIGGER IF NOT EXISTS {table}_new_identity_only BEFORE INSERT ON {table}
        WHEN EXISTS (SELECT 1 FROM version_identity WHERE row_id=NEW.row_id AND row_version=NEW.row_version)
        BEGIN SELECT RAISE(ABORT, 'a typed payload cannot attach to an existing version identity'); END""",
    )


def _identity_trigger(layout: Layout, *, generation_bound: bool = True) -> str:
    payload = f"EXISTS (SELECT 1 FROM {layout.version_table} WHERE row_id=NEW.row_id AND row_version=NEW.row_version)"
    if generation_bound:
        # A payload staged before retirement must not attach to another generation.
        # Empty unrelated histories permit the identity; only its own payload is required.
        condition = (f"((NEW.encoding='{layout.encoding}' AND NEW.payload_generation={layout.generation} AND NOT {payload}) OR "
                     f"({payload} AND (NEW.encoding<>'{layout.encoding}' OR NEW.payload_generation IS NOT {layout.generation})))")
    else:
        condition = f"NEW.encoding='{layout.encoding}' AND NOT {payload}"
    return f"""CREATE TRIGGER IF NOT EXISTS {layout.version_table}_identity BEFORE INSERT ON version_identity
        WHEN EXISTS (SELECT 1 FROM items WHERE row_id=NEW.row_id AND collection_id='{layout.collection_id}')
          AND {condition}
        BEGIN SELECT RAISE(ABORT, 'version_identity requires its unique generation-bound typed payload'); END"""


def _create_tables(conn, layout: Layout) -> None:
    current, _, _, _ = _declarations(layout)
    conn.execute(CreateTable(current))
    create_history(conn, layout)


def create_history(conn, layout: Layout) -> None:
    """Install one retained payload generation independently of its query relation."""
    if layout.encoding == TYPED_V2:
        from .native_tables import history

        version = history(layout)
    else:
        _, version, _, _ = _declarations(layout)
    conn.execute(CreateTable(version))
    for statement in _version_triggers(layout):
        conn.execute(statement)


def repair_triggers(conn, *, generation_bound: bool = True) -> None:
    """Restore each published typed collection's history protections (schema ensure)."""
    for cid, generation, encoded in conn.execute(
        "SELECT collection_id,generation,layout_json FROM typed_encoding_mappings WHERE state IN ('ready','retired')"
    ).fetchall():
        layout = _layout(cid, generation, encoded)
        for statement in (*_version_triggers(layout), _identity_trigger(layout, generation_bound=generation_bound)):
            conn.execute(statement)


def validate_history_bindings(conn) -> None:
    """Prove old typed history before schema 11 assigns its generation."""
    from . import tokens

    for cid, in conn.execute("SELECT DISTINCT i.collection_id FROM version_identity v JOIN items i ON i.row_id=v.row_id "
                             "WHERE v.encoding='typed-v1'"):
        if conn.execute("SELECT count(*) FROM typed_encoding_mappings WHERE collection_id=? AND state='ready'",
                        (cid,)).fetchone()[0] != 1:
            raise TypedStorageError("typed history has no unique ready descriptor")
        layout = ready_layout(conn, cid)
        if layout is None:
            raise TypedStorageError("typed history has no verified ready descriptor")
        for row_id, version, key, schema_version, payload in conn.execute(
            "SELECT v.row_id,v.row_version,i.item_key,v.schema_version,v.payload_hash FROM version_identity v "
            "JOIN items i ON i.row_id=v.row_id WHERE i.collection_id=? AND v.encoding='typed-v1'", (cid,),
        ):
            try:
                row = conn.execute(f"SELECT body,{','.join(layout.value_columns)},r FROM {layout.version_table} "
                                   "WHERE row_id=? AND row_version=?", (row_id, version)).fetchone()
                if row is None or tokens.payload_hash(schema_version, key, decode_row(layout, row[1:]), row[0]) != payload:
                    raise TypedStorageError("typed history payload does not prove its generation")
            except (sqlite3.DatabaseError, ValueError, TypeError, OverflowError) as error:
                raise TypedStorageError("typed history payload is absent or invalid") from error


# --- reads ------------------------------------------------------------------


def collection_encoding(conn, collection_id: str) -> str:
    row = conn.execute("SELECT encoding FROM collections WHERE collection_id=?", (collection_id,)).fetchone()
    if row is None:
        raise TypedStorageError("collection is absent")
    return row[0]


def ready_layout(conn, collection_id: str) -> Layout | None:
    row = conn.execute("SELECT generation,layout_json,layout_hash FROM typed_encoding_mappings "
                       "WHERE collection_id=? AND state='ready'", (collection_id,)).fetchone()
    if row is None:
        return None
    if hashlib.sha256(row[1].encode()).hexdigest() != row[2]:
        raise TypedStorageError("typed layout descriptor changed")
    layout = _layout(collection_id, row[0], row[1])
    if layout.encoding == TYPED_V2:
        from . import index_migrations
        from .native_projections import NativeProjectionPlan

        plan = index_migrations.ready_plan(conn, collection_id)
        if not isinstance(plan, NativeProjectionPlan) or plan.layout.generation != layout.generation:
            raise TypedStorageError("native payload has no matching published query relation")
        return replace(layout, current=plan.table_name)
    return layout


def require_layout(conn, collection_id: str) -> Layout:
    layout = ready_layout(conn, collection_id)
    if layout is None:
        raise TypedStorageError("typed collection has no published layout")
    return layout


def version_layout(conn, collection_id: str, generation: int) -> Layout:
    """Resolve the retained descriptor bound to an immutable version."""
    row = conn.execute("SELECT layout_json,layout_hash FROM typed_encoding_mappings WHERE collection_id=? "
                       "AND generation=? AND state IN ('ready','retired')", (collection_id, generation)).fetchone()
    if row is None or hashlib.sha256(row[0].encode()).hexdigest() != row[1]:
        raise TypedStorageError("typed version descriptor is absent or changed")
    return _layout(collection_id, generation, row[0])


def _current(conn, layout: Layout, where: str, parameters) -> list[tuple[int, str, dict[str, Any]]]:
    """Decode typed current rows selected on ``items i`` in one statement, refusing stale ones."""
    columns = ",".join(f"t.{column}" for column in (*layout.value_columns, "r"))
    rows = []
    for row_id, key, version, current, *values in conn.execute(
        f"SELECT i.row_id,i.item_key,i.row_version,t.row_version,{columns} FROM {layout.items} i "  # noqa: S608
        f"LEFT JOIN {layout.current_relation} t ON t.row_id=i.row_id WHERE i.collection_id=? AND {where}",
        (layout.collection_id, *parameters),
    ).fetchall():
        if version != current:
            raise TypedStorageError("typed current row is missing or stale")
        rows.append((row_id, key, decode_row(layout, values, connection=conn)))
    return rows


def current_values(conn, layout: Layout, row_id: int) -> dict[str, Any]:
    """Decode one typed current row of the layout's collection."""
    rows = _current(conn, layout, "i.row_id=?", (row_id,))
    if not rows:
        raise TypedStorageError("item is absent from its typed collection")
    return rows[0][2]


def selected_current_values(conn, layout: Layout, row_id: int, selection, *, max_bytes, check):
    """Select admitted typed columns before hydration; never fetch withheld residuals."""
    if layout.encoding == TYPED_V2:
        from . import native_rows

        return native_rows.selected_current_values(conn, layout, row_id, selection, max_bytes=max_bytes,
                                                   check=check, database=layout.database)
    from io import BytesIO

    from ..query_engine.selected_values import read_selected_tree
    from .field_admission import WHOLE_SUBTREE

    selected = [(ordinal, name) for ordinal, name in enumerate(layout.fields) if name in selection]
    columns = [f"t.{part}{ordinal}" for ordinal, _ in selected for part in "tvk"]
    residual = {name: tree for name, tree in selection.items() if name not in layout.fields}
    if residual:
        columns.append("t.r")
    values = conn.execute(
        f"SELECT i.row_version,t.row_version{',' if columns else ''}{','.join(columns)} FROM {layout.items} i "
        f"LEFT JOIN {layout.current_relation} t ON t.row_id=i.row_id WHERE i.row_id=? AND i.collection_id=?",
        (row_id, layout.collection_id),
    ).fetchone()
    if values is None or values[0] != values[1]:
        raise TypedStorageError("typed current row is missing or stale")
    result = {}
    for index, (_, name) in enumerate(selected):
        tag, value, key = values[2 + index * 3:5 + index * 3]
        if tag == JSON and selection[name] is not WHOLE_SUBTREE:
            decoded = read_selected_tree(BytesIO(value.encode()), selection[name], max_bytes=max_bytes, check=check)
        else:
            if selection[name] is not True and selection[name] is not WHOLE_SUBTREE and tag not in (MISSING, NULL):
                raise TypedStorageError("stored scalar does not match admitted field shape")
            decoded = decode_value(tag, value, key)
        if decoded is not _ABSENT:
            result[name] = decoded
    if residual and values[-1] is not None:
        result.update(read_selected_tree(BytesIO(values[-1].encode()), residual, max_bytes=max_bytes, check=check))
    return result


def item_values(conn, row_id: int) -> dict[str, Any]:
    """Current logical values of one item under its collection's single encoding."""
    row = conn.execute("SELECT collection_id,encoding,values_json FROM items WHERE row_id=?", (row_id,)).fetchone()
    if row is None:
        raise TypedStorageError("item is absent")
    if row[1] == JSON_V1:
        return json.loads(row[2])
    return current_values(conn, require_layout(conn, row[0]), row_id)


def collection_values(conn, collection_id: str) -> list[tuple[str, dict[str, Any]]]:
    """Every item's key and current values, read in one statement and decoded once."""
    if collection_encoding(conn, collection_id) == JSON_V1:
        return [(key, json.loads(raw)) for key, raw in conn.execute(
            "SELECT item_key, values_json FROM items WHERE collection_id = ?", (collection_id,))]
    return [(key, values) for _, key, values in _current(conn, require_layout(conn, collection_id), "1", ())]


_HYDRATE_BATCH = 500


def hydrate(conn, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give ``items`` row dicts their canonical ``values_json`` text without storing it.

    json-v1 rows are untouched. Typed rows are decoded with one statement per
    collection and batch of 500, never per row.
    """
    typed: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["encoding"] != JSON_V1:
            typed.setdefault(row["collection_id"], []).append(row)
    for collection_id, members in typed.items():
        layout = require_layout(conn, collection_id)
        for offset in range(0, len(members), _HYDRATE_BATCH):
            batch = {row["row_id"]: row for row in members[offset:offset + _HYDRATE_BATCH]}
            decoded = _current(conn, layout, f"i.row_id IN ({','.join('?' for _ in batch)})", tuple(batch))
            if len(decoded) != len(batch):
                raise TypedStorageError("item is absent from its typed collection")
            for row_id, _, values in decoded:
                batch[row_id]["values_json"] = canonical_json(values)
    return rows


def version_values(conn, row_id: int, row_version: int) -> tuple[dict[str, Any], str]:
    """Values and body of one immutable version, resolved through version_identity."""
    identity = conn.execute("SELECT v.encoding,i.collection_id,v.payload_generation FROM version_identity v "
                            "JOIN items i ON i.row_id=v.row_id WHERE v.row_id=? AND v.row_version=?",
                            (row_id, row_version)).fetchone()
    if identity is None:
        raise TypedStorageError("version identity is absent")
    if identity[0] == JSON_V1:
        raw, body = conn.execute("SELECT values_json,body FROM item_versions WHERE row_id=? AND row_version=?",
                                 (row_id, row_version)).fetchone()
        return json.loads(raw), body
    layout = version_layout(conn, identity[1], identity[2])
    row = conn.execute(f"SELECT body,{','.join(layout.value_columns)},r FROM {layout.version_table} "  # noqa: S608
                       "WHERE row_id=? AND row_version=?", (row_id, row_version)).fetchone()
    if row is None:
        raise TypedStorageError("typed version payload is absent")
    return decode_row(layout, row[1:], connection=conn), row[0]


# --- writes -----------------------------------------------------------------


def _current_parameters(conn, layout: Layout, row_id: int, row_version: int, encoded, *, item_key=None):
    values = {"row_id": row_id, "row_version": row_version,
              **dict(zip((*layout.value_columns, "r"), encoded, strict=True))}
    if layout.encoding == TYPED_V2:
        from . import native_identity

        if item_key is None:
            item_key = conn.execute("SELECT item_key FROM items WHERE row_id=? AND collection_id=?",
                                    (row_id, layout.collection_id)).fetchone()[0]
        values["item_key"] = native_identity.encode(item_key, layout.identity)
    return values


def _upsert_current(conn, layout: Layout, row_id: int, row_version: int, encoded, *, item_key=None) -> None:
    _, _, upsert, _ = _declarations(layout)
    conn.execute(upsert, _current_parameters(conn, layout, row_id, row_version, encoded, item_key=item_key))


def _version_parameters(layout, row_id, row_version, encoded, body, payload_hash, txn_id, schema_version):
    history = {"row_id": row_id, "row_version": row_version, "body": body,
               **dict(zip((*layout.value_columns, "r"), encoded, strict=True))}
    identity = {"row_id": row_id, "row_version": row_version,
                "encoding": layout.encoding, "payload_hash": payload_hash, "txn_id": txn_id,
                "schema_version": schema_version, "payload_generation": layout.generation}
    return history, identity


def write_version(conn, layout: Layout, *, row_id: int, row_version: int, values: Mapping[str, Any],
                  body: str, payload_hash: str, txn_id: int, schema_version: int, item_key=None, scope=None) -> None:
    """Write typed current/version payloads and their identity in the caller's transaction."""
    encoded = encode_row(layout, values, connection=conn, scope=scope)
    _upsert_current(conn, layout, row_id, row_version, encoded, item_key=item_key)
    _, _, _, insert_version = _declarations(layout)
    history, identity = _version_parameters(layout, row_id, row_version, encoded, body, payload_hash, txn_id, schema_version)
    conn.execute(insert_version, history)
    conn.execute(tables.INSERT_IDENTITY, identity)


def write_new_versions(conn, layout: Layout, rows, *, txn_id: int, schema_version: int, scope=None) -> None:
    """Encode new rows once, then install current payloads, history and identities."""
    current, history, identities = [], [], []
    for row_id, key, values, body, payload_hash in rows:
        encoded = encode_row(layout, values, connection=conn, scope=scope)
        current.append(_current_parameters(conn, layout, row_id, 1, encoded, item_key=key))
        version, identity = _version_parameters(layout, row_id, 1, encoded, body, payload_hash, txn_id, schema_version)
        history.append(version)
        identities.append(identity)
    _, _, upsert, insert_version = _declarations(layout)
    if layout.encoding == TYPED_V2:
        from .native_operands import FileRole, operand_snapshot

        raw = conn if isinstance(conn, sqlite3.Connection) else conn.connection
        with operand_snapshot(raw, layout, role=FileRole.STORE):
            conn.executemany(upsert, current)
    else:
        conn.executemany(upsert, current)
    # History must precede its identity: triggers refuse payloads attached to existing identities.
    conn.executemany(insert_version, history)
    conn.executemany(tables.INSERT_IDENTITY, identities)


# --- forward migration --------------------------------------------------------


def migration_diagnostic(conn, collection_id: str) -> dict | None:
    """The newest failed conversion's bounded reason; reads never restart that candidate."""
    row = conn.execute("SELECT state,diagnostic_json FROM typed_encoding_mappings WHERE collection_id=? "
                       "ORDER BY generation DESC LIMIT 1", (collection_id,)).fetchone()
    return json.loads(row[1]) if row is not None and row[0] == "failed" and row[1] is not None else None


def _transaction(conn):
    if not conn.in_transaction:
        raise RuntimeError("typed encoding migration requires a writer transaction")


def _copy(conn, layout: Layout, rows) -> bool:
    """Copy JSON current rows and prove each decodes to identical bytes and hash."""
    from . import tokens

    for row_id, row_version, key, schema_version, raw, body, payload in rows:
        _upsert_current(conn, layout, row_id, row_version, encode_row(layout, json.loads(raw)))
        try:
            decoded = current_values(conn, layout, row_id)
            proved = (canonical_json(decoded) == raw
                      and tokens.payload_hash(schema_version, key, decoded, body) == payload)
        except (TypedStorageError, ValueError, TypeError):
            proved = False
        if not proved:
            return False
    return True


def _fail(conn, layout: Layout) -> str:
    conn.execute(f"DROP TABLE {layout.version_table}")
    conn.execute(f"DROP TABLE {layout.current_table}")
    conn.execute("UPDATE typed_encoding_mappings SET state='failed' WHERE collection_id=? AND generation=?",
                 (layout.collection_id, layout.generation))
    return "failed"


_SOURCE_COLUMNS = "row_id,row_version,item_key,schema_version,values_json,body,payload_hash"


def migrate_batch(conn, collection_id: str, fields, *, limit: int = 128, encoding=TYPED_V1) -> str:
    """Advance one bounded forward migration step; return building, ready or failed.

    The caller owns the writer lease, transaction and collection authority.
    Batches copy current JSON rows in row_id order with per-row parity proof.
    The final step runs inside one writer transaction, so no write can
    interleave: it recopies rows changed since their copy, then publishes the
    collection encoding, item discriminators and layout atomically. Before that
    commit the JSON mapping stays the only authority; a failed proof drops the
    candidate tables and leaves it so. Old JSON history is never rewritten.
    """
    if encoding == TYPED_V2:
        from . import native_migrations

        return native_migrations.migrate_batch(conn, collection_id, fields, limit=limit)
    if encoding != TYPED_V1:
        raise ValueError("unsupported typed migration target")
    _transaction(conn)
    if type(limit) is not int or not 0 < limit <= 500:
        raise ValueError("typed migration limit must be between 1 and 500")
    if collection_encoding(conn, collection_id) == TYPED_V1:
        return "ready"
    row = conn.execute("SELECT generation,layout_json,last_row_id FROM typed_encoding_mappings "
                       "WHERE collection_id=? AND state='building'", (collection_id,)).fetchone()
    if row is None:
        generation = conn.execute("SELECT COALESCE(MAX(generation),0)+1 FROM typed_encoding_mappings "
                                  "WHERE collection_id=?", (collection_id,)).fetchone()[0]
        layout = Layout(collection_id, generation, tuple(sorted(fields)))
        _create_tables(conn, layout)
        conn.execute("INSERT INTO typed_encoding_mappings(collection_id,generation,state,layout_json,layout_hash) "
                     "VALUES(?,?,'building',?,?)", (collection_id, generation, layout.encoded,
                                                   hashlib.sha256(layout.encoded.encode()).hexdigest()))
        last = 0
    else:
        layout, last = _layout(collection_id, row[0], row[1]), row[2]
    rows = conn.execute(f"SELECT {_SOURCE_COLUMNS} FROM items WHERE collection_id=? AND row_id>? "  # noqa: S608
                        "ORDER BY row_id LIMIT ?", (collection_id, last, limit)).fetchall()
    if not _copy(conn, layout, rows):
        return _fail(conn, layout)
    if rows:
        last = rows[-1][0]
        conn.execute("UPDATE typed_encoding_mappings SET last_row_id=? WHERE collection_id=? AND generation=?",
                     (last, collection_id, layout.generation))
        if len(rows) == limit:
            return "building"
    # Final catch-up: rows written or changed after their batch copied them.
    stale = conn.execute(
        f"SELECT {','.join('i.' + name for name in _SOURCE_COLUMNS.split(','))} FROM items i "  # noqa: S608
        f"LEFT JOIN {layout.current_table} t ON t.row_id=i.row_id "
        "WHERE i.collection_id=? AND (t.row_id IS NULL OR t.row_version<>i.row_version) "
        "ORDER BY i.row_id LIMIT ?", (collection_id, limit + 1)).fetchall()
    if not _copy(conn, layout, stale[:limit]):
        return _fail(conn, layout)
    if len(stale) > limit:
        return "building"
    conn.execute("UPDATE collections SET encoding=? WHERE collection_id=?", (TYPED_V1, collection_id))
    conn.execute("UPDATE items SET encoding=?,values_json=NULL WHERE collection_id=?", (TYPED_V1, collection_id))
    conn.execute("UPDATE typed_encoding_mappings SET state='ready' WHERE collection_id=? AND generation=?",
                 (collection_id, layout.generation))
    conn.execute(_identity_trigger(layout))
    return "ready"
