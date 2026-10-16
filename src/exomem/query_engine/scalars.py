"""Exact typed scalar comparison keys, independent of SQLite numeric affinity."""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, replace
from decimal import Decimal

MISSING = object()
SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean", "date", "datetime", "enum", "link"})
MISSING_TAG, NULL_TAG, BOOLEAN_TAG, NUMBER_TAG = 0, 1, 2, 3
STRING_TAG, DATE_TAG, DATETIME_TAG, LINK_TAG = 4, 5, 6, 7
_OFFSET_GRAMMAR = r"Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9]"
_OFFSET = re.compile(_OFFSET_GRAMMAR)
_INSTANT = re.compile(
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})[Tt ]"
    r"(?P<time>[0-9]{2}:[0-9]{2}:[0-9]{2})(?:[.,](?P<fraction>[0-9]+))?"
    rf"(?P<offset>{_OFFSET_GRAMMAR})"
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
    fraction = matched["fraction"]
    if fraction is None and value[10] == "T":
        # Already the text rebuilt below; base scans parse one per row.
        text = value
    else:
        fraction = fraction or ""
        if any(digit != "0" for digit in fraction[6:]):
            raise ScalarValueError("datetime precision exceeds exact microseconds")
        text = f"{matched['date']}T{matched['time']}"
        if fraction:
            text += "." + fraction[:6]
        text += matched["offset"]
    try:
        return dt.datetime.fromisoformat(text).astimezone(dt.UTC)
    except (ValueError, OverflowError) as error:
        raise ScalarValueError("scalar instant is invalid") from error


def offset_minutes(value: object) -> int:
    """Minutes east of UTC for a typed-v1 offset: ``Z`` or ``±HH:MM``."""
    if type(value) is not str or not _OFFSET.fullmatch(value):
        raise ScalarValueError("offset requires Z or a ±HH:MM UTC offset")
    if value == "Z":
        return 0
    minutes = int(value[1:3]) * 60 + int(value[4:6])
    return -minutes if value[0] == "-" else minutes


def instant_offset_minutes(value: object) -> int:
    """The UTC offset, in minutes, carried by a valid typed-v1 instant string."""
    parse_instant(value)
    return offset_minutes(_INSTANT.fullmatch(value)["offset"])


@dataclass(frozen=True, slots=True)
class InstantSpelling:
    """Reconstruction fields from this module's single fixed instant grammar."""

    separator: str
    fraction_separator: str
    fraction_digits: int
    offset: str


def instant_spelling(value: object) -> tuple[dt.datetime, InstantSpelling]:
    """Return the existing query instant and its exact lexical representation."""
    instant = parse_instant(value)
    matched = _INSTANT.fullmatch(value)
    fraction = matched["fraction"]
    return instant, InstantSpelling(value[10], value[19] if fraction is not None else "", len(fraction or ""), matched["offset"])


def validate_instant_spelling(spelling: InstantSpelling) -> None:
    """Validate reconstruction tokens through the sole grammar without padding a long fraction."""
    if (type(spelling.separator) is not str or type(spelling.fraction_separator) is not str
            or type(spelling.fraction_digits) is not int or spelling.fraction_digits < 0 or type(spelling.offset) is not str):
        raise ScalarValueError("invalid instant reconstruction format")
    # This closed-protocol placeholder stays within range under every admitted offset.
    text = "2000-01-02" + spelling.separator + "00:00:00"
    if spelling.fraction_digits:
        text += spelling.fraction_separator + "0"
    _, actual = instant_spelling(text + spelling.offset)
    if actual != replace(spelling, fraction_digits=1 if spelling.fraction_digits else 0):
        raise ScalarValueError("invalid instant reconstruction format")


def number_key_bytes_v1(value: int | float) -> bytes:
    """Encode the pinned number-key v1 wire shared by comparison and payloads.

    Typed-v2 overflow values make this wire authoritative. A format change
    needs another version; it must retain this encoder and its inverse.
    """
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ScalarValueError("number-key v1 requires an integer or finite number")
    decimal = Decimal(value) if type(value) is int else Decimal.from_float(value)
    sign, digits, exponent = decimal.as_tuple()
    if not any(digits):
        return b"\1"
    adjusted = len(digits) + exponent - 1
    end = len(digits)
    while digits[end - 1] == 0:
        end -= 1
    # Decimal construction/as_tuple are exact even under a low-precision
    # ambient context. No arithmetic or normalize() may round the coefficient.
    magnitude = (adjusted + 2**63).to_bytes(8, "big")
    magnitude += bytes(digit + 1 for digit in digits[:end]) + b"\0"
    if sign:
        return b"\0" + bytes(255 - byte for byte in magnitude)
    return b"\2" + magnitude


def decode_number_overflow_v1(key: bytes, *, as_float: bool, digit_budget: int) -> int | float:
    """Decode an integer-valued overflow key under the caller's digit budget.

    Outside SQLite's signed-64 range, every finite binary64 value is integral.
    The v1 sign bands, biased exponent and digit bytes are fixed wire values.
    """
    if type(digit_budget) is not int or digit_budget < 1 or type(as_float) is not bool:
        raise ScalarValueError("number-key v1 requires a decoding budget and subtype")
    if type(key) is not bytes or len(key) < 11 or key[0] not in (0, 2):
        raise ScalarValueError("invalid number-key v1 overflow frame")
    # V1 has one band byte, eight exponent bytes and one terminator byte.
    if len(key) - 10 > digit_budget:
        raise ScalarValueError("number-key v1 exceeds the decoding budget")
    negative = key[0] == 0
    magnitude = bytes(255 - byte for byte in key[1:]) if negative else key[1:]
    digits = magnitude[8:-1]
    if (magnitude[-1] != 0 or not digits or any(not 1 <= byte <= 10 for byte in digits)
            or digits[0] == 1 or digits[-1] == 1):
        raise ScalarValueError("invalid number-key v1 coefficient")
    adjusted = int.from_bytes(magnitude[:8], "big") - 2**63
    exponent = adjusted - len(digits) + 1
    if exponent < 0 or len(digits) + exponent > digit_budget:
        raise ScalarValueError("number-key v1 exceeds the decoding budget")
    coefficient = 0
    for byte in digits:
        coefficient = coefficient * 10 + byte - 1
    integer = coefficient * 10**exponent
    if negative:
        integer = -integer
    try:
        value = float(integer) if as_float else integer
    except OverflowError as error:
        raise ScalarValueError("number-key v1 does not encode a finite number") from error
    if (as_float and not math.isfinite(value)) or number_key_bytes_v1(value) != key:
        raise ScalarValueError("number-key v1 is not an exact canonical value")
    return value


def _number_key(value: int | float) -> str:
    return number_key_bytes_v1(value).hex()


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
