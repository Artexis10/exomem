"""Exact typed scalar comparison keys, independent of SQLite numeric affinity."""

from __future__ import annotations

import datetime as dt
import math
import re
from decimal import Decimal

MISSING = object()
SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean", "date", "datetime", "enum", "link"})
MISSING_TAG, NULL_TAG, BOOLEAN_TAG, NUMBER_TAG = 0, 1, 2, 3
STRING_TAG, DATE_TAG, DATETIME_TAG, LINK_TAG = 4, 5, 6, 7
_INSTANT = re.compile(
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})[Tt ]"
    r"(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})(?:[.,](?P<fraction>[0-9]+))?"
    r"(?P<offset>Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)


class ScalarValueError(ValueError):
    code = "QUERY_VALUE_INVALID"


def parse_instant(value: object) -> dt.datetime:
    """Parse typed-v1 extended ISO seconds with an exact UTC/minute offset.

    Only microsecond precision is representable; extra zero digits are exact,
    but nonzero tails, basic forms and second-resolution offsets are refused.
    Stored canonical strings and the legacy parser are not rewritten.
    """
    matched = _INSTANT.fullmatch(value) if type(value) is str else None
    if matched is None:
        raise ScalarValueError("datetime requires extended ISO seconds and a UTC/minute offset")
    fraction = matched["fraction"] or ""
    if any(digit != "0" for digit in fraction[6:]):
        raise ScalarValueError("datetime precision exceeds exact microseconds")
    text = f"{matched['date']}T{matched['time']}"
    if fraction:
        text += "." + fraction[:6]
    try:
        return dt.datetime.fromisoformat(text + matched["offset"]).astimezone(dt.UTC)
    except (ValueError, OverflowError) as error:
        raise ScalarValueError("scalar instant is invalid") from error


def _number_key(value: int | float) -> str:
    decimal = Decimal(value) if type(value) is int else Decimal.from_float(value)
    sign, digits, exponent = decimal.as_tuple()
    if not any(digits):
        return "01"
    adjusted = len(digits) + exponent - 1
    end = len(digits)
    while digits[end - 1] == 0:
        end -= 1
    # Decimal construction/as_tuple are exact even under a low-precision
    # ambient context. No arithmetic or normalize() may round the coefficient.
    magnitude = (adjusted + 2**63).to_bytes(8, "big")
    magnitude += bytes(digit + 1 for digit in digits[:end]) + b"\0"
    if sign:
        return (b"\0" + bytes(255 - byte for byte in magnitude)).hex()
    return (b"\2" + magnitude).hex()


def scalar_key(value: object, kind: str) -> tuple[int, str]:
    """Return a presence/type tag and a binary-order-preserving hex payload.

    Missing and null have distinct tags. Enum values share the bool, numeric
    and string domains without Python's bool/int equality or string coercion.
    The payload is safe for JSON1 TEXT extraction, including embedded NULs.
    """
    if kind not in SCALAR_TYPES:
        raise ScalarValueError("a declared scalar type is required")
    if value is MISSING:
        return MISSING_TAG, ""
    if value is None:
        return NULL_TAG, ""
    if kind == "enum":
        kind = {bool: "boolean", int: "number", float: "number", str: "string"}.get(type(value))
    if kind == "boolean" and type(value) is bool:
        return BOOLEAN_TAG, "01" if value else "00"
    if kind in {"integer", "number"}:
        if type(value) is int or (kind == "number" and type(value) is float and math.isfinite(value)):
            return NUMBER_TAG, _number_key(value)
    if kind in {"string", "link"} and type(value) is str:
        try:
            return (STRING_TAG if kind == "string" else LINK_TAG), value.encode("utf-8").hex()
        except UnicodeEncodeError as error:
            raise ScalarValueError("scalar text must be valid UTF-8") from error
    if kind in {"date", "datetime"} and type(value) is str:
        try:
            if kind == "date":
                return DATE_TAG, dt.date.fromisoformat(value).toordinal().to_bytes(4, "big").hex()
            delta = parse_instant(value) - dt.datetime(1, 1, 1, tzinfo=dt.UTC)
            micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
            return DATETIME_TAG, micros.to_bytes(8, "big").hex()
        except (ValueError, OverflowError) as error:
            raise ScalarValueError("scalar date or instant is invalid") from error
    raise ScalarValueError("value does not match its declared scalar type")
