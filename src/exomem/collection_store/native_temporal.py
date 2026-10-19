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
from .native_metadata import MEMO_ENTRIES, FieldMetadata
from .typed_storage import TypedStorageError

# Closed temporal wire: version byte, signed-64 delta, positive signed-64 format ID.
_FRAME = struct.Struct(">Bqq")
_VERSION = 1
_INITIAL_FORMAT = 1
# The four fixed spelling fields, including a signed-64 width, fit in 256 JSON bytes.
_FORMAT_BYTES = 256
_ORIGIN = dt.datetime(1, 1, 1, tzinfo=dt.UTC)
_MICROSECONDS_PER_DAY = 86400 * 1000000
_COORDINATE_LIMIT = dt.date.max.toordinal() * _MICROSECONDS_PER_DAY
_UNIX_COORDINATE = (dt.date(1970, 1, 1).toordinal() - 1) * _MICROSECONDS_PER_DAY
_DEFAULT_INSTANT = InstantSpelling("T", "", 0, "Z")


class TemporalView(NamedTuple):
    """One validated native value before optional lexical reconstruction."""

    coordinate: int
    local: dt.date | dt.datetime
    spelling: InstantSpelling | None
    text_bytes: int

    def instant_order(self) -> int:
        """Return an instant's exact microseconds since the Unix epoch."""
        return self.coordinate - _UNIX_COORDINATE

    def instant_day(self, minutes: int) -> dt.date:
        """Apply a supplied offset, preserving datetime's boundary overflow."""
        shifted = self.coordinate + minutes * 60 * 1000000
        if not 0 <= shifted < _COORDINATE_LIMIT:
            raise OverflowError("date value out of range")
        return _local_day(shifted)


class _ReconstructionFormat(NamedTuple):
    spelling: InstantSpelling
    zone: dt.tzinfo
    quantum: int
    text_bytes: int
    shift: int
    #: The coordinates whose instant and local time both lie inside the calendar: ``low <= coordinate < high``.
    low: int
    high: int


def _coordinate(instant: dt.datetime) -> int:
    delta = instant - _ORIGIN
    return delta.days * _MICROSECONDS_PER_DAY + delta.seconds * 1000000 + delta.microseconds


def _local_instant(coordinate: int, spelling: InstantSpelling, compiled=None, *, day_only=False) -> dt.date | dt.datetime:
    compiled = _compiled_spelling(spelling) if compiled is None else compiled
    return _view(coordinate, compiled, None, day_only).local


def _view(coordinate: int, compiled: _ReconstructionFormat, max_bytes: int | None, day_only: bool) -> TemporalView:
    """The one rule set for a native instant: budget, then calendar range, then the format's precision."""
    if max_bytes is not None and compiled.text_bytes > max_bytes:
        raise TypedStorageError("native instant exceeds the decoding budget")
    if not compiled.low <= coordinate < compiled.high:
        raise OverflowError("date value out of range")
    local = coordinate + compiled.shift
    if local % compiled.quantum:
        raise TypedStorageError("native instant precision cannot represent its coordinate")
    when = (_local_day(local) if day_only
            else (_ORIGIN + dt.timedelta(microseconds=coordinate)).astimezone(compiled.zone))
    # TemporalView.__new__ builds exactly this tuple; calling it directly skips one Python frame per cell.
    return tuple.__new__(TemporalView, (coordinate, when, compiled.spelling, compiled.text_bytes))


def _local_day(local: int) -> dt.date:
    return dt.date.fromordinal(local // _MICROSECONDS_PER_DAY + 1)


def _text_length(spelling: InstantSpelling) -> int:
    return 19 + len(spelling.offset) + (1 + spelling.fraction_digits if spelling.fraction_digits else 0)


def _render(coordinate: int, spelling: InstantSpelling, max_bytes: int, compiled=None) -> str:
    length = _text_length(spelling)
    if length > max_bytes:
        raise TypedStorageError("native instant exceeds the decoding budget")
    local = _local_instant(coordinate, spelling, compiled)
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


def _compiled_spelling(spelling: InstantSpelling) -> _ReconstructionFormat:
    minutes = offset_minutes(spelling.offset)
    shift = minutes * 60 * 1000000
    return _ReconstructionFormat(spelling, dt.timezone(dt.timedelta(minutes=minutes)),
                                 10 ** max(0, 6 - spelling.fraction_digits), _text_length(spelling), shift,
                                 max(0, -shift), min(_COORDINATE_LIMIT, _COORDINATE_LIMIT - shift))


def _compile_format(raw: object) -> _ReconstructionFormat:
    return _compiled_spelling(_format(raw))


def _unpack(cell: object) -> tuple[int, int]:
    if type(cell) is int:
        return cell, _INITIAL_FORMAT
    if type(cell) is bytes and len(cell) == _FRAME.size:
        version, delta, format_id = _FRAME.unpack(cell)
        if version == _VERSION and format_id > _INITIAL_FORMAT:
            return delta, format_id
    raise TypedStorageError("native instant cell or frame is invalid")


def instant_days(metadata: FieldMetadata):
    """Return one scan's day-only instant readers, ``(inline, accept)``, for one datetime field.

    ``accept(cell, max_bytes)`` is ``inspect`` itself, and it learns each format it accepts. ``inline(cell,
    max_bytes)`` is the scan's hot copy of ``_unpack`` and ``_view`` above, kept beside them: for a native cell of
    an accepted format it returns what they return, and None wherever they would refuse. Change the three
    together; the warm-scan parity test compares ``inline`` with ``inspect``.
    """
    epoch = None
    formats = {}  # accepted format IDs of this field, the initial format included
    frame_size, unpack, day, view = _FRAME.size, _FRAME.unpack, dt.date.fromordinal, tuple.__new__

    def inline(cell: object, max_bytes: int) -> TemporalView | None:
        kind = type(cell)
        if kind is int:
            delta, compiled = cell, formats.get(_INITIAL_FORMAT)
        elif kind is bytes and len(cell) == frame_size:
            version, delta, format_id = unpack(cell)
            compiled = formats.get(format_id) if version == _VERSION and format_id > _INITIAL_FORMAT else None
        else:
            return None
        if compiled is None or compiled.text_bytes > max_bytes:
            return None
        coordinate = epoch + delta
        if not compiled.low <= coordinate < compiled.high:
            return None
        local = coordinate + compiled.shift
        if local % compiled.quantum:
            return None
        return view(TemporalView, (coordinate, day(local // _MICROSECONDS_PER_DAY + 1), compiled.spelling,
                                   compiled.text_bytes))

    def accept(cell: object, max_bytes: int) -> TemporalView | str | None:
        nonlocal epoch
        found = inspect(cell, "datetime", metadata, max_bytes=max_bytes, day_only=True)
        if type(found) is TemporalView and len(formats) < MEMO_ENTRIES:
            # Accepted in this snapshot, so the append-only epoch and this format stay readable.
            epoch, format_id = metadata.epoch(), _unpack(cell)[1]
            formats[format_id] = metadata.temporal_format(format_id, max_bytes=_FORMAT_BYTES)
        return found

    return inline, accept


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
    default = metadata.temporal_format(_INITIAL_FORMAT, max_bytes=_FORMAT_BYTES)
    if coordinate is None:
        return original
    initial = spelling == default.spelling
    if _render(coordinate, spelling, len(original.encode("utf-8")), default if initial else None) != original:
        return original
    if initial:
        return coordinate - epoch
    # A frozen spelling of instant_spelling's str and int fields: equal spellings give identical format JSON.
    return _FRAME.pack(_VERSION, coordinate - epoch, metadata.intern_as(spelling, asdict))


def inspect(cell: object, kind: str, metadata: FieldMetadata, *, max_bytes: int | None,
            day_only: bool = False) -> TemporalView | str | None:
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
        return _view(epoch + delta, metadata.temporal_format(format_id, max_bytes=_FORMAT_BYTES), max_bytes, day_only)
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
