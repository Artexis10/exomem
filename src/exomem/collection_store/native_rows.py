"""Lossless native rows, including values admitted after a schema revision."""

from __future__ import annotations

import datetime as dt
import json
import struct
from collections.abc import Mapping
from io import BytesIO

from ..query_engine.scalars import ScalarValueError
from . import native_enum, native_numbers, native_temporal
from .native_layout import MASK_BITS, Codec, NativeLayout
from .native_metadata import FieldMetadata, WriteScope, snapshot_fields
from .typed_storage import _ABSENT, TypedStorageError, canonical_json

# Closed escape wire: reserved prefix plus canonical JSON, disjoint from scalar frames.
ESCAPE_PREFIX = b"\xff\x01"
_json_string = json.encoder.encode_basestring


def encoded_equal(left: tuple, right: tuple) -> bool:
    """Compare authoritative cells without merging numeric subtype or signed zero."""
    if len(left) != len(right):
        return False
    return all(type(a) is type(b) and (struct.pack(">d", a) == struct.pack(">d", b) if type(a) is float else a == b)
               for a, b in zip(left, right, strict=True))


def _representable(codec: Codec, value: object) -> bool:
    match codec:
        case Codec.TEXT:
            return type(value) is str
        case Codec.NUMBER:
            return type(value) in (int, float)
        case Codec.BOOLEAN:
            return type(value) is bool
        case Codec.DATE | Codec.DATETIME:
            if type(value) not in (str, dt.date, dt.datetime):
                return False
            if codec is Codec.DATE and type(value) is dt.datetime:
                return False
            if codec is Codec.DATETIME and type(value) is dt.date:
                return False
            if type(value) is str:
                try:
                    if codec is Codec.DATE:
                        dt.date.fromisoformat(value)
                    else:
                        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    return False
            return True
        case Codec.ENUM:
            return type(value) in (str, int, float, bool)
        case Codec.JSON:
            return True
    raise TypedStorageError("unsupported native field codec")


def encode_cell(codec: Codec, value: object, metadata: FieldMetadata):
    """Encode validated values; corruption or failed encoding never becomes an escape."""
    if value is None:
        return None
    if not _representable(codec, value):
        return ESCAPE_PREFIX + canonical_json(value).encode("utf-8")
    try:
        match codec:
            case Codec.TEXT:
                return value
            case Codec.NUMBER:
                return native_numbers.encode(value)
            case Codec.BOOLEAN:
                return int(value)
            case Codec.DATE | Codec.DATETIME:
                return native_temporal.encode(value, "date" if codec is Codec.DATE else "datetime", metadata)
            case Codec.ENUM:
                return native_enum.encode(value, metadata)
            case Codec.JSON:
                return canonical_json(value)
    except ScalarValueError as error:
        raise TypedStorageError("invalid native numeric value") from error
    raise TypedStorageError("unsupported native field codec")


def _json_value(payload: bytes, *, max_bytes: int):
    if len(payload) > max_bytes:
        raise TypedStorageError("native JSON exceeds the decoding budget")
    try:
        value = json.loads(payload)
        if canonical_json(value).encode("utf-8") != payload:
            raise TypedStorageError("native JSON is not canonical")
        return value
    except (ValueError, TypeError, OverflowError, RecursionError) as error:
        raise TypedStorageError("invalid native JSON value") from error


def _text(cell: str, max_bytes: int) -> str:
    # An ASCII string's UTF-8 length is its length, so most cells skip encoding.
    if (len(cell) if cell.isascii() else len(cell.encode("utf-8"))) > max_bytes:
        raise TypedStorageError("native text exceeds the decoding budget")
    return cell


def decode_cell(codec: Codec, cell: object, metadata: FieldMetadata, *, max_bytes: int):
    if type(max_bytes) is not int or max_bytes < 0:
        raise TypedStorageError("native decoding requires a byte budget")
    if cell is None:
        return None
    if type(cell) is bytes and cell.startswith(ESCAPE_PREFIX):
        if len(cell) - len(ESCAPE_PREFIX) > max_bytes:
            raise TypedStorageError("native JSON exceeds the decoding budget")
        return _json_value(cell[len(ESCAPE_PREFIX):], max_bytes=max_bytes)
    try:
        match codec:
            case Codec.TEXT if type(cell) is str:
                return _text(cell, max_bytes)
            case Codec.NUMBER:
                return native_numbers.decode(cell, digit_budget=max_bytes)
            case Codec.BOOLEAN if type(cell) is int and cell in (0, 1):
                return bool(cell)
            case Codec.DATE | Codec.DATETIME:
                return native_temporal.decode(cell, "date" if codec is Codec.DATE else "datetime", metadata,
                                              max_bytes=max_bytes)
            case Codec.ENUM if type(cell) is int:
                return native_enum.decode(cell, metadata, max_bytes=max_bytes)
            case Codec.JSON if type(cell) is str:
                return _json_value(cell.encode("utf-8"), max_bytes=max_bytes)
    except (ScalarValueError, UnicodeError) as error:
        raise TypedStorageError("invalid native scalar cell") from error
    raise TypedStorageError("native cell does not match its frozen codec")


def encode_row(connection, layout: NativeLayout, values: Mapping, *, scope: WriteScope | None = None) -> tuple:
    """Encode one row; a writer transaction encoding many rows passes its own scope to share metadata reads."""
    # Measure validated source values before compaction; frames cannot authorize their own expansion.
    extent = len(canonical_json(values).encode("utf-8"))
    masks = [0] * len(layout.mask_columns)
    cells = []
    for ordinal, (name, codec) in enumerate(zip(layout.fields, layout.codecs, strict=True)):
        value = values.get(name, _ABSENT)
        if value is _ABSENT:
            masks[ordinal // MASK_BITS] |= 1 << (ordinal % MASK_BITS)
            cells.append(None)
        else:
            metadata = (FieldMetadata(connection, layout.collection_id, layout.generation, ordinal) if scope is None
                        else scope.field(connection, layout.collection_id, layout.generation, ordinal))
            cells.append(encode_cell(codec, value, metadata))
    known = set(layout.fields)
    residual = {name: value for name, value in values.items() if name not in known}
    return (extent, *masks, *cells, canonical_json(residual) if residual else None)


def _masks(layout: NativeLayout, columns) -> tuple:
    count = len(layout.mask_columns)
    if len(columns) != count + len(layout.fields) + 2:
        raise TypedStorageError("native row width differs from its layout")
    masks = tuple(columns[1:1 + count])
    for ordinal, mask in enumerate(masks):
        width = min(MASK_BITS, len(layout.fields) - ordinal * MASK_BITS)
        if type(mask) is not int or not 0 <= mask < 1 << width:
            raise TypedStorageError("invalid native missing mask")
    return masks


def _add_value(values: dict, name: str, value, remaining: int) -> int:
    remaining -= len(canonical_json({name: value}).encode("utf-8")) - 2 + bool(values)
    if remaining < 0:
        raise TypedStorageError("native values exceed the decoding budget")
    values[name] = value
    return remaining


def decode_row(connection, layout: NativeLayout, columns, *, max_bytes: int | None = None, database: str = "main") -> dict:
    columns = tuple(columns)
    masks = _masks(layout, columns)
    extent = columns[0]
    if type(extent) is not int or extent < 2:
        raise TypedStorageError("native row requires its measured values extent")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < extent):
        raise TypedStorageError("native row exceeds the decoding budget")
    values = {}
    remaining = extent - 2

    for ordinal, (name, codec) in enumerate(zip(layout.fields, layout.codecs, strict=True)):
        cell = columns[1 + len(masks) + ordinal]
        if masks[ordinal // MASK_BITS] & (1 << (ordinal % MASK_BITS)):
            if cell is not None:
                raise TypedStorageError("missing native field has a payload")
            continue
        metadata = FieldMetadata(connection, layout.collection_id, layout.generation, ordinal, database)
        remaining = _add_value(values, name, decode_cell(codec, cell, metadata, max_bytes=remaining), remaining)
    residual = columns[-1]
    if residual is not None:
        if type(residual) is not str:
            raise TypedStorageError("native residual must be JSON text")
        extra = _json_value(residual.encode("utf-8"), max_bytes=remaining + 2)
        if type(extra) is not dict or set(extra) & set(layout.fields):
            raise TypedStorageError("native residual duplicates declared fields")
        for name, value in extra.items():
            remaining = _add_value(values, name, value, remaining)
    if remaining != 0:
        raise TypedStorageError("native row does not match its measured values extent")
    return values


def selected_cell(codec: Codec, cell: object, metadata: FieldMetadata, selection, *, max_bytes: int, check):
    """Apply field-tree admission inside JSON before constructing its values."""
    payload = (cell[len(ESCAPE_PREFIX):] if type(cell) is bytes and cell.startswith(ESCAPE_PREFIX)
               else cell.encode("utf-8") if codec is Codec.JSON and type(cell) is str else None)
    if payload is not None:
        from ..query_engine.selected_values import read_selected_tree
        from .native_json import canonical_events

        # Validate physical JSON bytes without imposing the whole row's extent on a selected result.
        return read_selected_tree(BytesIO(payload), selection, max_bytes=max_bytes, check=check,
                                  events=canonical_events(payload, max_bytes=len(payload), check=check))
    if selection is not True and cell is not None:
        from .field_admission import WHOLE_SUBTREE

        if selection is not WHOLE_SUBTREE:
            raise TypedStorageError("stored scalar does not match admitted field shape")
    return decode_cell(codec, cell, metadata, max_bytes=max_bytes)


def _selected_masks(layout, masks, values):
    result = dict(zip(masks, values, strict=True))
    for ordinal, mask in result.items():
        width = min(MASK_BITS, len(layout.fields) - ordinal * MASK_BITS)
        if type(mask) is not int or not 0 <= mask < 1 << width:
            raise TypedStorageError("invalid native missing mask")
    return result


def prepare_dense_selection(connection, layout: NativeLayout, selection, *, max_bytes: int, check,
                            semantic_fields=frozenset()):
    """Share one bounded physical decoder across an admitted scalar scan."""
    from .field_admission import WHOLE_SUBTREE

    if type(max_bytes) is not int or max_bytes < 2:
        raise TypedStorageError("native selected values require a byte budget")
    if any(name not in layout.fields or tree is not True and tree is not WHOLE_SUBTREE
           or layout.codecs[layout.fields.index(name)] is Codec.JSON for name, tree in selection.items()):
        return None
    selected = [(ordinal, name) for ordinal, name in enumerate(layout.fields) if name in selection]
    masks = tuple(sorted({ordinal // MASK_BITS for ordinal, _ in selected}))
    # The measured whole-row extent guards escaped containers before SQLite returns any payload.
    columns = ["i.row_id", "i.row_version", "t.row_version", "t.values_bytes",
               *(f"t.m{ordinal}" for ordinal in masks),
               *(f"CASE WHEN t.values_bytes BETWEEN 2 AND {max_bytes} THEN t.v{ordinal} END"
                 for ordinal, _ in selected)]
    metadata = snapshot_fields(connection, layout.collection_id, layout.generation,
                               (ordinal for ordinal, _ in selected), database=layout.database)
    # Row positions: four identity and extent columns, the selected masks, then one cell per selected field.
    cells_at = 4 + len(masks)
    mask_bounds = tuple((4 + index, 1 << min(MASK_BITS, len(layout.fields) - ordinal * MASK_BITS))
                        for index, ordinal in enumerate(masks))
    slots = []
    for index, (ordinal, name) in enumerate(selected):
        codec, semantic = layout.codecs[ordinal], name in semantic_fields
        inline, accept = (native_temporal.instant_days(metadata[ordinal]) if semantic and codec is Codec.DATETIME
                          else (None, None))
        # A name's charge includes its colon and one separating comma; the decoder starts one byte up for the first.
        slots.append((name, 4 + masks.index(ordinal // MASK_BITS), 1 << (ordinal % MASK_BITS),
                      len(canonical_json(name).encode("utf-8")) + 2, cells_at + index,
                      codec is Codec.NUMBER, codec is Codec.TEXT, inline,
                      (codec, metadata[ordinal], selection[name], semantic, accept)))
    plain, prefix, view_type = native_numbers.plain, ESCAPE_PREFIX, native_temporal.TemporalView
    single_mask = mask_bounds[0][1] if len(mask_bounds) == 1 else 0

    def decode(row):
        extent = row[3]
        if row[1] != row[2] or type(extent) is not int or extent < 2:
            raise TypedStorageError("native current row is missing, stale or has no measured extent")
        if single_mask:
            mask = row[4]
            if type(mask) is not int or not 0 <= mask < single_mask:
                raise TypedStorageError("invalid native missing mask")
        else:
            for at, width in mask_bounds:
                mask = row[at]
                if type(mask) is not int or not 0 <= mask < width:
                    raise TypedStorageError("invalid native missing mask")
        if extent > max_bytes:
            return None
        result, remaining = {}, extent - 1
        for name, mask_at, missing_bit, name_bytes, at, number, text, inline, shared in slots:
            cell = row[at]
            if row[mask_at] & missing_bit:
                if cell is not None:
                    raise TypedStorageError("missing native field has a payload")
                continue
            # Each shortcut runs the owner's own rule for its common cell; everything else goes to the shared decoders.
            if inline is not None and type(cell) is not str and (value := inline(cell, extent)) is not None:
                pass  # an instant whose format this scan accepted; an escape has no frame's version byte
            elif number and plain(cell):
                value = cell  # native_numbers.decode returns a plain cell itself
            elif text and type(cell) is str and cell.isascii():
                value = _text(cell, extent)  # ASCII cannot fail to encode, so decode_cell's UnicodeError wrap never applies
            else:
                codec, field_metadata, tree, semantic, accept = shared
                escaped = type(cell) is bytes and cell.startswith(prefix)
                if cell is None:
                    value = None
                elif accept is not None and not escaped:
                    value = accept(cell, extent)
                elif semantic and not escaped:
                    value = native_temporal.inspect(cell, "date" if codec is Codec.DATE else "datetime",
                                                    field_metadata, max_bytes=extent, day_only=True)
                elif not escaped:
                    # Eligibility already proves whole scalar admission; keep the shared codec as decoder authority.
                    value = decode_cell(codec, cell, field_metadata, max_bytes=extent)
                else:
                    if len(cell) - len(prefix) > extent:
                        raise TypedStorageError("native JSON exceeds its measured values extent")
                    value = selected_cell(codec, cell, field_metadata, tree, max_bytes=max_bytes, check=check)
            # Canonical JSON bytes of the value; a TemporalView counts its full spelling.
            kind = type(value)
            if kind is int:
                size = len(str(value))
            elif kind is view_type:
                size = value.text_bytes + 2
            elif kind is str:
                # The canonical serializer uses this same non-ASCII JSON string encoder; ASCII JSON is a byte a character.
                encoded = _json_string(value)
                size = len(encoded) if encoded.isascii() else len(encoded.encode("utf-8"))
            elif value is None:
                size = 4
            elif kind is bool:
                size = 4 if value else 5
            else:
                size = len(canonical_json(value).encode("utf-8"))
            remaining -= name_bytes + size
            if remaining < 0:
                raise TypedStorageError("native values exceed the decoding budget")
            result[name] = value
        return result

    return ",".join(columns), decode


def _stream_selected_json(connection, layout, row_id, column, selection, *, extent, max_bytes, check, prefix=0,
                          database):
    from ..query_engine.selected_values import read_selected_tree
    from .native_json import canonical_events

    table = layout.current_table.rsplit(".", 1)[-1].strip('"')
    with (connection.blobopen(table, column, row_id, readonly=True, name=database) as source,
          connection.blobopen(table, column, row_id, readonly=True, name=database) as comparison):
        size = len(source) - prefix
        if size < 0 or size > extent:
            raise TypedStorageError("native JSON exceeds its measured values extent")
        source.seek(prefix)
        comparison.seek(prefix)
        return read_selected_tree(source, selection, max_bytes=max_bytes, check=check,
                                  events=canonical_events(source, max_bytes=extent, check=check,
                                                          comparison=comparison, byte_length=size))


def selected_current_values(connection, layout: NativeLayout, row_id: int, selection, *, max_bytes: int, check,
                            database: str = "main") -> dict:
    """Fetch admitted scalars only; stream containers before nested projection."""
    if type(max_bytes) is not int or max_bytes < 2:
        raise TypedStorageError("native selected values require a byte budget")
    prepared = prepare_dense_selection(connection, layout, selection, max_bytes=max_bytes, check=check)
    if prepared is not None:
        columns, decode = prepared
        check()
        physical = connection.execute(
            f"SELECT {columns} FROM {layout.items} i "
            f"LEFT JOIN {layout.current_relation} t ON t.row_id=i.row_id "
            "WHERE i.row_id=? AND i.collection_id=?", (row_id, layout.collection_id)).fetchone()
        check()
        if physical is None:
            raise TypedStorageError("native current row is missing")
        result = decode(physical)
        if result is not None:
            return result
    selected = [(ordinal, name) for ordinal, name in enumerate(layout.fields) if name in selection]
    masks = tuple(sorted({ordinal // MASK_BITS for ordinal, _ in selected}))
    columns = ["t.values_bytes", *(f"t.m{ordinal}" for ordinal in masks)]
    for ordinal, _ in selected:
        # Closed SQLite storage types discriminate scalar cells from streamed containers.
        cell = "NULL" if layout.codecs[ordinal] is Codec.JSON else (
            f"CASE WHEN typeof(t.v{ordinal})='blob' THEN NULL ELSE t.v{ordinal} END")
        columns.extend((cell, f"typeof(t.v{ordinal})"))
    residual = {name: tree for name, tree in selection.items() if name not in layout.fields}
    if residual:
        columns.append("typeof(t.r)")
    check()
    row = connection.execute(
        f"SELECT i.row_version,t.row_version{',' if columns else ''}{','.join(columns)} "
        f"FROM {layout.items} i LEFT JOIN {layout.current_relation} t ON t.row_id=i.row_id "
        "WHERE i.row_id=? AND i.collection_id=?", (row_id, layout.collection_id),
    ).fetchone()
    check()
    if row is None or row[0] != row[1]:
        raise TypedStorageError("native current row is missing or stale")
    extent = row[2]
    if type(extent) is not int or extent < 2:
        raise TypedStorageError("native row requires its measured values extent")
    mask_values = _selected_masks(layout, masks, row[3:3 + len(masks)])
    result = {}
    scalar_budget = min(max_bytes, extent)
    remaining = scalar_budget - 2
    for index, (ordinal, name) in enumerate(selected):
        check()
        mask = mask_values[ordinal // MASK_BITS]
        cell, kind = row[3 + len(masks) + 2 * index:5 + len(masks) + 2 * index]
        if mask & (1 << (ordinal % MASK_BITS)):
            if kind != "null":
                raise TypedStorageError("missing native field has a payload")
            continue
        metadata = FieldMetadata(connection, layout.collection_id, layout.generation, ordinal, database)
        codec = layout.codecs[ordinal]
        if kind == "blob":
            table = layout.current_table.rsplit(".", 1)[-1].strip('"')
            with connection.blobopen(table, f"v{ordinal}", row_id, readonly=True, name=database) as blob:
                prefix = blob.read(len(ESCAPE_PREFIX))
                escaped = prefix == ESCAPE_PREFIX
                if not escaped:
                    if len(blob) > scalar_budget:
                        raise TypedStorageError("native scalar exceeds the decoding budget")
                    blob.seek(0)
                    cell = blob.read()
            if escaped:
                value = _stream_selected_json(connection, layout, row_id, f"v{ordinal}", selection[name],
                                              extent=extent, max_bytes=scalar_budget, check=check,
                                              prefix=len(ESCAPE_PREFIX), database=database)
            else:
                value = selected_cell(codec, cell, metadata, selection[name], max_bytes=scalar_budget, check=check)
        elif codec is Codec.JSON and kind != "null":
            if kind != "text":
                raise TypedStorageError("native JSON must be text")
            value = _stream_selected_json(connection, layout, row_id, f"v{ordinal}", selection[name],
                                          extent=extent, max_bytes=scalar_budget, check=check, database=database)
        else:
            value = selected_cell(codec, cell, metadata, selection[name], max_bytes=scalar_budget, check=check)
        remaining = _add_value(result, name, value, remaining)
    if residual and row[-1] != "null":
        if row[-1] != "text":
            raise TypedStorageError("native residual must be JSON text")
        extra = _stream_selected_json(connection, layout, row_id, "r", residual,
                                      extent=extent, max_bytes=scalar_budget, check=check, database=database)
        if type(extra) is not dict or set(extra) & set(layout.fields):
            raise TypedStorageError("native residual duplicates declared fields")
        for name, value in extra.items():
            remaining = _add_value(result, name, value, remaining)
    return result
