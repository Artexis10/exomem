"""Integer temporal cells with exact lexical reconstruction and fallback."""

import datetime as dt
import struct
from dataclasses import asdict
from typing import NamedTuple

from .. import structured_collections as collections
from ..query_engine.scalars import (
    InstantSpelling,
    ScalarValueError,
    instant_spelling,
    offset_minutes,
    parse_instant,
    validate_instant_spelling,
)
from .native_metadata import FieldMetadata
from .typed_storage import TypedStorageError

# Closed temporal wire: version byte, signed-64 delta, positive signed-64 format ID.
_FRAME = struct.Struct(">Bqq")
_VERSION = 1
_INITIAL_FORMAT = 1
# The four fixed spelling fields, including a signed-64 width, fit in 256 JSON bytes.
_FORMAT_BYTES = 256
_ORIGIN = dt.datetime(1, 1, 1, tzinfo=dt.UTC)
_MICROSECONDS_PER_DAY = 86400 * 1000000
_DEFAULT_INSTANT = InstantSpelling("T", "", 0, "Z")


class TemporalView(NamedTuple):
    """One validated native value before optional lexical reconstruction."""

    coordinate: int
    local: dt.date | dt.datetime
    spelling: InstantSpelling | None
    text_bytes: int


class _ReconstructionFormat(NamedTuple):
    spelling: InstantSpelling
    zone: dt.tzinfo
    quantum: int
    text_bytes: int


def _coordinate(instant: dt.datetime) -> int:
    delta = instant - _ORIGIN
    return delta.days * _MICROSECONDS_PER_DAY + delta.seconds * 1000000 + delta.microseconds


def _local_instant(coordinate: int, spelling: InstantSpelling, compiled=None) -> dt.datetime:
    instant = _ORIGIN + dt.timedelta(microseconds=coordinate)
    zone = compiled.zone if compiled is not None else dt.timezone(dt.timedelta(minutes=offset_minutes(spelling.offset)))
    local = instant.astimezone(zone)
    quantum = compiled.quantum if compiled is not None else 10 ** max(0, 6 - spelling.fraction_digits)
    if local.microsecond % quantum:
        raise TypedStorageError("native instant precision cannot represent its coordinate")
    return local


def _text_length(spelling: InstantSpelling) -> int:
    return 19 + len(spelling.offset) + (1 + spelling.fraction_digits if spelling.fraction_digits else 0)


def _render(coordinate: int, spelling: InstantSpelling, max_bytes: int) -> str:
    length = _text_length(spelling)
    if length > max_bytes:
        raise TypedStorageError("native instant exceeds the decoding budget")
    local = _local_instant(coordinate, spelling)
    text = f"{local.date().isoformat()}{spelling.separator}{local.hour:02}:{local.minute:02}:{local.second:02}"
    if spelling.fraction_digits:
        fraction = f"{local.microsecond:06}"
        text += spelling.fraction_separator + (fraction[:spelling.fraction_digits]
                                               if spelling.fraction_digits <= 6 else fraction + "0" * (spelling.fraction_digits - 6))
    text += spelling.offset
    # The source parser or _format validates tokens; _local_instant proves range and precision.
    return text


def _format(raw: object) -> InstantSpelling:
    if not isinstance(raw, dict):
        raise TypedStorageError("native instant format is invalid")
    try:
        spelling = InstantSpelling(**raw)
        validate_instant_spelling(spelling)
        return spelling
    except (TypeError, ScalarValueError) as error:
        raise TypedStorageError("native instant format is invalid") from error


def _compile_format(raw: object) -> _ReconstructionFormat:
    spelling = _format(raw)
    return _ReconstructionFormat(spelling, dt.timezone(dt.timedelta(minutes=offset_minutes(spelling.offset))),
                                 10 ** max(0, 6 - spelling.fraction_digits), _text_length(spelling))


def _unpack(cell: object) -> tuple[int, int]:
    if type(cell) is int:
        return cell, _INITIAL_FORMAT
    if type(cell) is bytes and len(cell) == _FRAME.size:
        version, delta, format_id = _FRAME.unpack(cell)
        if version == _VERSION and format_id > _INITIAL_FORMAT:
            return delta, format_id
    raise TypedStorageError("native instant cell or frame is invalid")


def encode(value: object, kind: str, metadata: FieldMetadata) -> int | bytes | str | None:
    """Keep one authoritative cell; compact only an exactly reconstructible spelling."""
    if value is None:
        return None
    if kind == "date":
        original = value if type(value) is str else collections._normalize_date(value)
        parsed = dt.date.fromisoformat(original)
        epoch = metadata.epoch()
        if epoch is None:
            epoch = metadata.ensure_epoch(parsed.toordinal())
        return parsed.toordinal() - epoch if parsed.isoformat() == original else original
    if kind != "datetime":
        raise TypedStorageError("unknown native temporal codec")
    original = value if type(value) is str else collections._normalize_datetime(value)
    coordinate = None
    spelling = None
    try:
        instant, spelling = instant_spelling(original)
        coordinate = _coordinate(instant)
    except ScalarValueError:
        # Storage accepts more ISO spellings than indexed query instants.
        collections._normalize_datetime(original)
    epoch = metadata.epoch()
    if epoch is None:
        # Zero is a numeric compression origin for lexical-first fields, never a timezone assignment.
        epoch = metadata.ensure_epoch(0 if coordinate is None else coordinate)
        if metadata.intern(asdict(spelling or _DEFAULT_INSTANT)) != _INITIAL_FORMAT:
            raise TypedStorageError("native instant metadata has no initial format")
    default = _format(metadata.format(_INITIAL_FORMAT, max_bytes=_FORMAT_BYTES))
    if coordinate is None:
        return original
    if _render(coordinate, spelling, len(original.encode("utf-8"))) != original:
        return original
    if spelling == default:
        return coordinate - epoch
    return _FRAME.pack(_VERSION, coordinate - epoch, metadata.intern(asdict(spelling)))


def inspect(cell: object, kind: str, metadata: FieldMetadata, *, max_bytes: int | None) -> TemporalView | str | None:
    """Share frame, format, range and precision validation before rendering text."""
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise TypedStorageError("native temporal decoding requires a byte budget")
    if cell is None:
        return None
    try:
        if type(cell) is str:
            if max_bytes is not None and len(cell.encode("utf-8")) > max_bytes:
                raise TypedStorageError("native temporal value exceeds the decoding budget")
            if kind == "date":
                collections._normalize_date(cell)
            elif kind == "datetime":
                collections._normalize_datetime(cell)
            else:
                raise TypedStorageError("unknown native temporal codec")
            return cell
        epoch = metadata.epoch()
        if epoch is None:
            raise TypedStorageError("native temporal epoch is absent")
        if kind == "date" and type(cell) is int:
            local = dt.date.fromordinal(epoch + cell)
            if max_bytes is not None and 10 > max_bytes:
                raise TypedStorageError("native date exceeds the decoding budget")
            return TemporalView(epoch + cell, local, None, 10)
        if kind != "datetime":
            raise TypedStorageError("native temporal cell does not match its codec")
        delta, format_id = _unpack(cell)
        compiled = metadata.temporal_format(format_id, max_bytes=_FORMAT_BYTES)
        spelling, length = compiled.spelling, compiled.text_bytes
        if max_bytes is not None and length > max_bytes:
            raise TypedStorageError("native instant exceeds the decoding budget")
        return TemporalView(epoch + delta, _local_instant(epoch + delta, spelling, compiled), spelling, length)
    except (ValueError, TypeError, OverflowError, collections.CollectionError) as error:
        raise TypedStorageError("native temporal reconstruction is invalid") from error


def decode(cell: object, kind: str, metadata: FieldMetadata, *, max_bytes: int) -> str | None:
    """Bound reconstruction before allocation and refuse unknown or damaged frames."""
    if type(max_bytes) is not int or max_bytes < 0:
        raise TypedStorageError("native temporal decoding requires a byte budget")
    value = inspect(cell, kind, metadata, max_bytes=max_bytes)
    if not isinstance(value, TemporalView):
        return value
    if value.spelling is None:
        return value.local.isoformat()
    return _render(value.coordinate, value.spelling, max_bytes)


def operand(cell: object, kind: str, metadata: FieldMetadata, *, max_bytes: int) -> int:
    """Apply the existing query parser; lexical storage never widens query admission."""
    if type(cell) is str:
        value = decode(cell, kind, metadata, max_bytes=max_bytes)
        if kind == "date":
            return dt.date.fromisoformat(value).toordinal()
        if kind == "datetime":
            return _coordinate(parse_instant(value))
    else:
        # Comparisons need no lexical allocation; selected values still require their full byte budget.
        value = inspect(cell, kind, metadata, max_bytes=None)
        if isinstance(value, TemporalView):
            return value.coordinate
    raise TypedStorageError("native temporal comparison does not match its codec")
