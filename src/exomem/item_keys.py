"""Item key schemes: how a derived row's natural key becomes its public item key.

Two schemes exist, and the store records one per collection at creation (``collections.item_key_scheme``).

``hashed-v1`` is the key every collection had before ordered keys: ``uuid5(collection, serialized natural key)``,
built by ``structured_collections.inferred_item_key``. It is random in key order.

``ordered-v1`` is an injective, order-preserving byte encoding of the natural key, so rows that arrive in key
order are inserted at the end of every index that ends with the key. Each natural-key field becomes one
component, in declared order. A component starts with a presence byte, ``0x01`` for a value and ``0x02`` for
null, so nulls sort last, then the value. Null is not ``0xFF`` because ``0xFF`` follows ``0x00`` inside a string:
with ``0xFF`` as presence byte, ``("a", null, "p\\0\\x01q")`` and ``("a\\0\\x01p", null, "q")`` encode alike.

* ``date``: 3 bytes, days since 1970-01-01 as offset-binary (days plus 2**23), big-endian.
* ``datetime``: 8 bytes, UTC microseconds since the epoch as offset-binary (plus 2**63), big-endian, then 2 bytes,
  the UTC offset in minutes plus 32768. A datetime with no offset reads its wall clock as UTC and takes the
  offset bytes ``0x0000``, which no offset in minutes can produce, so it differs from the same clock at ``+00:00``.
* ``integer``: the FoundationDB tuple-layer integer encoding (see ``integer``).
* ``number``: the IEEE 754 binary64 with its bits order-transformed, then ``0x00`` for a JSON float, or ``0x01``
  and the integer encoding for a JSON integer, so ``5`` and ``5.0`` stay distinct. Order follows the binary64
  value first, so an integer and a float that round to the same binary64 value (above 2**53) order by their tail.
* ``boolean``: ``0x00`` or ``0x01``.
* ``string``, ``enum``, ``link``: NFC UTF-8 with each ``0x00`` written as ``0x00 0xFF``, then ``0x00``.

An incomplete natural key (no identity from the row itself) encodes as ``0xFE``, the first 16 bytes of its
member's SHA-256, then the integer encoding of the row's position in the member. ``0xFE`` is no presence byte,
so it never equals a complete key, and it sorts after every complete key.

A key longer than 320 bytes keeps its first 288 bytes and appends the 32-byte SHA-256 of the serialized natural
key. The public spelling of a key is lowercase, unpadded RFC 4648 base32hex: its alphabet ascends in ASCII and
the encoding is a big-endian bit stream, so text order equals byte order, prefixes included. Its longest
spelling, 512 characters, is the item key limit.

Every byte of this encoding is frozen by tests: a change would re-key stored rows.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import math
import struct
import unicodedata
from bisect import bisect_left
from collections.abc import Callable, Sequence
from typing import Any

# The two schemes are the closed set that the store's CHECK constraint fixes; they are wire values.
HASHED_V1 = "hashed-v1"
ORDERED_V1 = "ordered-v1"

#: A stored ordered key is 1 to this many bytes (the store's CHECK), whose spelling is 512 characters.
MAX_KEY_BYTES = 320
#: A longer key keeps this many bytes, then the 32-byte digest of its serialized natural key.
_KEPT_BYTES = MAX_KEY_BYTES - 32

_PRESENT, _NULL, _INCOMPLETE = b"\x01", b"\x02", b"\xfe"
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_EPOCH_DATE = dt.date(1970, 1, 1)
# Tuple-layer integer sizes: 2**(8n)-1 for n bytes. Codes are 0x14 plus or minus n; longer integers use 0x1D and 0x0B.
_SIZE_LIMITS = tuple((1 << (8 * n)) - 1 for n in range(9))
_ZERO, _LONG_POSITIVE, _LONG_NEGATIVE = 0x14, 0x1D, 0x0B
_NAIVE_OFFSET = b"\x00\x00"
_B32_PAD = {0: "", 2: "======", 4: "====", 5: "===", 7: "="}


def integer(value: int) -> bytes:
    """The FoundationDB tuple-layer encoding of an integer: ordered, and self-delimiting.

    Zero is ``0x14``. An integer of up to 8 bytes takes ``0x14 + n`` (``0x14 - n`` when negative) and ``n``
    big-endian bytes, which are the one's complement for a negative one. A longer one takes ``0x1D`` (``0x0B``)
    and a length byte (complemented for a negative one). A length over 255 bytes is refused.
    """
    if type(value) is not int:
        raise ValueError("an integer component needs an integer")
    if value == 0:
        return bytes((_ZERO,))
    magnitude = abs(value)
    if magnitude >= _SIZE_LIMITS[-1]:
        length = (magnitude.bit_length() + 7) // 8
        if length > 255:
            raise ValueError("a natural key integer is limited to 255 bytes")
        if value > 0:
            return bytes((_LONG_POSITIVE, length)) + value.to_bytes(length, "big")
        return bytes((_LONG_NEGATIVE, length ^ 0xFF)) + (value + (1 << (8 * length)) - 1).to_bytes(length, "big")
    size = bisect_left(_SIZE_LIMITS, magnitude)
    if value > 0:
        return bytes((_ZERO + size,)) + value.to_bytes(size, "big")
    return bytes((_ZERO - size,)) + (_SIZE_LIMITS[size] + value).to_bytes(size, "big")


def _number(value: Any) -> bytes:
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValueError("a number component needs an integer or a finite float")
    try:
        bits = int.from_bytes(struct.pack(">d", value), "big")
    except OverflowError as error:
        raise ValueError("a natural key number must fit a binary64") from error
    bits = bits ^ ((1 << 64) - 1) if bits >> 63 else bits | (1 << 63)
    return bits.to_bytes(8, "big") + (b"\x00" if type(value) is float else b"\x01" + integer(value))


def _text(value: Any) -> bytes:
    if type(value) is not str:
        raise ValueError("a text component needs a string")
    return unicodedata.normalize("NFC", value).encode("utf-8").replace(b"\x00", b"\x00\xff") + b"\x00"


def _date(value: Any) -> bytes:
    day = dt.date.fromisoformat(value) if type(value) is str else value
    if type(day) is not dt.date:
        raise ValueError("a date component needs an ISO date")
    return ((day - _EPOCH_DATE).days + (1 << 23)).to_bytes(3, "big")


def _datetime(value: Any) -> bytes:
    moment = dt.datetime.fromisoformat(value) if type(value) is str else value
    if type(moment) is not dt.datetime:
        raise ValueError("a datetime component needs an ISO 8601 datetime")
    offset = moment.utcoffset()
    if offset is None:
        micros, tail = (moment - _EPOCH.replace(tzinfo=None)) // dt.timedelta(microseconds=1), _NAIVE_OFFSET
    else:
        minutes, remainder = divmod(offset, dt.timedelta(minutes=1))
        if remainder:
            raise ValueError("a natural key offset must be a whole number of minutes")
        micros, tail = (moment - _EPOCH) // dt.timedelta(microseconds=1), (minutes + 32768).to_bytes(2, "big")
    return (micros + (1 << 63)).to_bytes(8, "big") + tail


def _boolean(value: Any) -> bytes:
    if type(value) is not bool:
        raise ValueError("a boolean component needs a boolean")
    return b"\x01" if value else b"\x00"


def _encoder(field_type: str) -> Callable[[Any], bytes] | None:
    """The value encoder of a field type, or None when ``ordered-v1`` has none for it."""
    # Field types are the schema validator's fixed tokens (as in native_layout.Codec.for_kind), not vocabulary.
    match field_type:
        case "string" | "enum" | "link":
            return _text
        case "integer":
            return integer
        case "number":
            return _number
        case "boolean":
            return _boolean
        case "date":
            return _date
        case "datetime":
            return _datetime
    return None


def encodes(field_type: str, options: Sequence[Any] = ()) -> bool:
    """Whether a natural-key field of this type and enum options has an ``ordered-v1`` encoding.

    ``array`` and ``object`` have none. An enum is text, so one that admits a non-string value has none either.
    """
    if _encoder(field_type) is None:
        return False
    return field_type != "enum" or all(type(option) is str for option in options)


def _component(field_type: str, value: Any) -> bytes:
    if value is None:
        return _NULL
    encoder = _encoder(field_type)
    if encoder is None:
        raise ValueError("a natural key field has a scalar type")
    return _PRESENT + encoder(value)


def ordered_key(components: Sequence[tuple[str, Any]], serialized: Callable[[], str]) -> bytes:
    """The ``ordered-v1`` key of a complete natural key.

    ``components`` are the natural-key fields' ``(field type, value)`` in declared order, with values as
    ``structured_collections`` normalises them. ``serialized`` returns the natural key's JSON serialization,
    which only a key over the byte limit reads. A natural key of no fields has no identity and is refused.
    """
    if not components:
        raise ValueError("a natural key needs at least one field")
    key = b"".join(_component(field_type, value) for field_type, value in components)
    if len(key) > MAX_KEY_BYTES:
        key = key[:_KEPT_BYTES] + hashlib.sha256(serialized().encode("utf-8")).digest()
    return key


def member_position_key(member_sha256: str, position: int) -> bytes:
    """The ``ordered-v1`` key of a row whose natural key is incomplete, from its member and its position."""
    return _INCOMPLETE + bytes.fromhex(member_sha256)[:16] + integer(position)


def spell(key: bytes) -> str:
    """The public spelling of an ordered key: lowercase, unpadded base32hex."""
    if type(key) is not bytes or not 0 < len(key) <= MAX_KEY_BYTES:
        raise ValueError("an ordered item key is 1 to 320 bytes")
    return base64.b32hexencode(key).decode("ascii").rstrip("=").lower()


def unspell(text: str) -> bytes:
    """The bytes of a public spelling, or ValueError when ``text`` is not the one spelling of some ordered key."""
    if type(text) is not str or not text.isascii():
        raise ValueError("an ordered item key is lowercase base32hex")
    try:
        key = base64.b32hexdecode(text.upper() + _B32_PAD.get(len(text) % 8, "?"))
    except ValueError as error:  # binascii.Error is one
        raise ValueError("an ordered item key is lowercase base32hex") from error
    if spell(key) != text:
        raise ValueError("an ordered item key has one canonical spelling")
    return key
