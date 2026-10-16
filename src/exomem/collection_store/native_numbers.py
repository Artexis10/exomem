"""One authoritative numeric cell for the typed-v2 encoding."""

from __future__ import annotations

import math

from ..query_engine.scalars import (
    ScalarValueError,
    decode_number_overflow_v1,
    number_key_bytes_v1,
)

# SQLite signed-64 bounds and the typed-v2 frame's closed subtype bytes.
LOW, HIGH = -(2**63), 2**63 - 1
INTEGER, FLOAT = 0, 1
WIRE = "number-key-v1"


def encode(value: int | float) -> int | float | bytes:
    """Keep ordinary values in STRICT ANY; frame only values outside its band."""
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ScalarValueError("a numeric cell requires an integer or finite number")
    if LOW <= value <= HIGH:
        return value
    subtype = INTEGER if type(value) is int else FLOAT
    return bytes((subtype,)) + number_key_bytes_v1(value)


def plain(cell: object) -> bool:
    """Whether a cell is an ordinary INTEGER or REAL, its own value; NaN and infinities fall outside the band."""
    return (type(cell) is int or type(cell) is float) and LOW <= cell <= HIGH


def decode(cell: int | float | bytes, *, digit_budget: int) -> int | float:
    """Restore exact subtype; reject malformed frames before they become values."""
    if plain(cell):
        return cell
    if type(cell) in (int, float):
        raise ScalarValueError("ordinary numeric cell is outside its band")
    if type(cell) is not bytes or len(cell) < 12 or cell[0] not in (INTEGER, FLOAT):
        raise ScalarValueError("invalid typed-v2 numeric frame")
    value = decode_number_overflow_v1(cell[1:], as_float=cell[0] == FLOAT, digit_budget=digit_budget)
    if LOW <= value <= HIGH or encode(value) != cell:
        raise ScalarValueError("overflow numeric cell is not its canonical value")
    return value
