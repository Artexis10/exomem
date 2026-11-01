"""Versioned comparison operands over one authoritative native payload cell."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from io import BytesIO
from types import MappingProxyType

from ..query_engine.scalars import (
    BOOLEAN_TAG,
    DATE_TAG,
    DATETIME_TAG,
    LINK_TAG,
    MISSING,
    MISSING_TAG,
    NULL_TAG,
    NUMBER_TAG,
    STRING_TAG,
    ScalarValueError,
    scalar_key,
)
from . import native_numbers, native_rows, native_temporal
from .native_layout import Codec
from .native_metadata import FieldMetadata, snapshot_fields
from .typed_storage import TypedStorageError

# Function version, components and file roles are closed persisted wire values.
FUNCTION = "exomem_native_operand_v1"
COMPONENTS = ("tag", "band", "key")  # nosemgrep: ep-word-set -- The versioned operand function fixes these output components.


class FileRole(StrEnum):
    STORE = "store"
    DERIVED = "derived"


@dataclass(frozen=True, slots=True)
class _MetadataScope:
    connection: sqlite3.Connection
    role: FileRole
    database: str
    collection_id: str
    generation: int
    field_readers: Mapping[int, object]


_metadata_scope: ContextVar[_MetadataScope | None] = ContextVar("native_operand_metadata", default=None)


@contextmanager
def operand_snapshot(connection, layout, *, role):
    """Reuse metadata for one synchronous payload operation after its final metadata write.

    Never span async work or more than one payload operation.
    """
    readers = snapshot_fields(connection, layout.collection_id, layout.generation, range(len(layout.fields)),
                              database=layout.database)
    scope = _MetadataScope(connection, FileRole(role), layout.database, layout.collection_id, layout.generation,
                           MappingProxyType(readers))
    token = _metadata_scope.set(scope)
    try:
        yield
    finally:
        _metadata_scope.reset(token)


def family(kind: str) -> str:
    # An indexed integer can widen to number without replacing its generated DDL.
    return "number" if kind == "integer" else kind


def ranked_operand(value: object, kind: str) -> tuple[int, int, int, object]:
    """A scalar operand with its null-placement rank: values, then null, then missing, ascending."""
    tag, band, key = scalar_operand(value, kind)
    return (2 if tag == MISSING_TAG else 1 if tag == NULL_TAG else 0), tag, band, key


def scalar_operand(value: object, kind: str) -> tuple[int, int, object]:
    """Use the same comparison domains for literals, released values and cells."""
    if value is MISSING:
        return MISSING_TAG, 0, ""
    if value is None:
        return NULL_TAG, 0, ""
    if kind == "enum":
        # These Python types are the query protocol's scalar domains, not enum members.
        kind = {bool: "boolean", int: "number", float: "number", str: "string"}.get(type(value))
    if kind in ("integer", "number"):
        if type(value) is not int and not (kind == "number" and type(value) is float):
            raise ScalarValueError("value does not match its numeric operand family")
        cell = native_numbers.encode(value)
        if type(cell) is bytes:
            # The subtype byte preserves payload fidelity, never numeric comparison order.
            return NUMBER_TAG, cell[1], cell[1:]
        return NUMBER_TAG, 1, cell
    if kind == "boolean" and type(value) is bool:
        return BOOLEAN_TAG, 0, int(value)
    if kind in ("string", "link") and type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeError as error:
            raise ScalarValueError("scalar text must be valid UTF-8") from error
        return STRING_TAG if kind == "string" else LINK_TAG, 0, value
    # Reuse the sole date/instant parser and its exact existing query admission.
    tag, encoded = scalar_key(value, kind)
    if tag in (DATE_TAG, DATETIME_TAG):
        return tag, 0, int.from_bytes(bytes.fromhex(encoded), "big")
    raise ScalarValueError("value does not match its declared scalar type")


@lru_cache(maxsize=128)
def _path(encoded: str) -> tuple[str | int, ...]:
    """Cache only immutable DDL arguments, never values or reconstruction metadata."""
    value = json.loads(encoded)
    if type(value) is not list or any(type(part) is not str and not (type(part) is int and part >= 0) for part in value):
        raise TypedStorageError("native operand requires a declared relative path")
    return tuple(value)


def _selected(codec, cell, metadata, path, extent):
    if not path:
        return native_rows.decode_cell(codec, cell, metadata, max_bytes=extent)
    from ..query_engine.selected_values import read_scalar_path
    from .native_json import canonical_events

    if type(cell) is bytes and cell.startswith(native_rows.ESCAPE_PREFIX):
        if len(cell) - len(native_rows.ESCAPE_PREFIX) > extent:
            raise TypedStorageError("native JSON exceeds the decoding budget")
        payload = cell[len(native_rows.ESCAPE_PREFIX):]
    elif codec is Codec.JSON and type(cell) is str:
        payload = cell.encode("utf-8")
    else:
        return MISSING
    def check():
        pass
    return read_scalar_path(BytesIO(payload), path, max_bytes=extent, check=check,
                            events=canonical_events(payload, max_bytes=extent, check=check))


def cell_operand(codec: Codec, cell: object, metadata: FieldMetadata, operand_family: str,
                 path: tuple[str | int, ...], *, values_bytes: int) -> tuple[int, int, object]:
    if type(values_bytes) is not int or values_bytes < 2:
        raise TypedStorageError("native operand requires the measured values extent")
    escaped = type(cell) is bytes and cell.startswith(native_rows.ESCAPE_PREFIX)
    if not escaped and not path:
        if codec is Codec.NUMBER and operand_family == "number":
            value = native_numbers.decode(cell, digit_budget=values_bytes)
            if type(cell) is bytes:
                return NUMBER_TAG, cell[1], cell[1:]
            return NUMBER_TAG, 1, value
        if codec in (Codec.DATE, Codec.DATETIME) and operand_family == ("date" if codec is Codec.DATE else "datetime"):
            value = native_temporal.operand(cell, operand_family, metadata, max_bytes=values_bytes)
            return DATE_TAG if codec is Codec.DATE else DATETIME_TAG, 0, value
    value = _selected(codec, cell, metadata, path, values_bytes)
    return scalar_operand(value, operand_family)


def register_native_operands(conn: sqlite3.Connection, *, databases: Mapping[FileRole, str]) -> None:
    """Bind physical roles once; aliases never choose a payload's metadata file."""
    roles = MappingProxyType({FileRole(role): database for role, database in databases.items()})
    if any(type(database) is not str or not database for database in roles.values()):
        raise ValueError("native operand roles require database namespaces")

    def operand(codec_id, collection_id, generation, ordinal, operand_family, path_json, role, cell, extent, component):
        physical_role = FileRole(role)
        database = roles[physical_role]
        if component not in COMPONENTS:
            raise TypedStorageError("unknown native operand component")
        scope = _metadata_scope.get()
        metadata = None
        if (scope is not None and scope.connection is conn and scope.role is physical_role
                and scope.database == database and scope.collection_id == collection_id
                and scope.generation == generation):
            metadata = scope.field_readers.get(ordinal)
        if metadata is None:
            metadata = FieldMetadata(conn, collection_id, generation, ordinal, database)
        parts = cell_operand(Codec(codec_id), cell, metadata, operand_family, _path(path_json), values_bytes=extent)
        return parts[COMPONENTS.index(component)]

    conn.create_function(FUNCTION, 10, operand, deterministic=True)
