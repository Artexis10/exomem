"""Frozen binary identity codecs without changing accepted key spelling."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from .. import item_keys

# These identifiers describe the closed typed-v2 wire, not collection vocabulary.
UUID16 = "uuid16"
UTF8 = "utf8"
# The raw bytes of an ``ordered-v1`` item key; the public spelling is item_keys.spell.
ORDERED = "ordered"
# Canonical UUID text and unsigned big-endian UUID bytes have the same order.
_UUID_LIMIT = 1 << 128


def choose(keys: Iterable[str], *, canonical_uuid_ingress: bool) -> str:
    """Select UUID16 only under a future-ingress guarantee and complete scan.

    Existing collection admission validates keys. A sample or empty current
    collection cannot establish the caller's future insertion contract.
    """
    if not canonical_uuid_ingress:
        return UTF8
    for key in keys:
        try:
            if str(UUID(key)) != key:
                return UTF8
        except (ValueError, AttributeError):
            return UTF8
    return UUID16


def encode(key: str, codec: str) -> bytes:
    """Encode an already admitted identity under its frozen descriptor."""
    if codec == UTF8:
        return key.encode("utf-8")
    if codec == UUID16:
        identity = UUID(key)
        if str(identity) != key:
            raise ValueError("UUID16 requires a canonical identity")
        return identity.bytes
    if codec == ORDERED:
        return item_keys.unspell(key)
    raise ValueError("unsupported native identity codec")


def decode(cell: bytes, codec: str) -> str:
    """Restore the original public spelling; never normalize opaque identities."""
    if type(cell) is not bytes:
        raise ValueError("a native identity must be a BLOB")
    if codec == UTF8:
        return cell.decode("utf-8")
    if codec == UUID16:
        return str(UUID(bytes=cell))
    if codec == ORDERED:
        return item_keys.spell(cell)
    raise ValueError("unsupported native identity codec")


def uuid_cuts(literal: str) -> tuple[int, int]:
    """Return lower/upper order cuts for any admitted string, without UUID aliasing.

    The closed UUID domain needs at most 129 bisection comparisons. Its upper
    sentinel is an integer cut, never an identity that can become a stored key.
    """
    bound = literal.encode("utf-8")
    lower, upper = 0, _UUID_LIMIT
    while lower < upper:
        middle = (lower + upper) // 2
        if str(UUID(int=middle)).encode("ascii") < bound:
            lower = middle + 1
        else:
            upper = middle
    exact = lower < _UUID_LIMIT and str(UUID(int=lower)).encode("ascii") == bound
    return lower, lower + int(exact)
