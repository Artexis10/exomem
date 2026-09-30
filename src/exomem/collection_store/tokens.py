"""Domain-separated tokens for the structured-collection store (design §3, §16 A3).

Every token is a lowercase 64-hex SHA-256 digest over a domain tag and
NUL-separated components, so no two token kinds can collide and no component
can smuggle a separator. Integers are rendered in decimal ASCII.
"""

from __future__ import annotations

import hashlib
import re

_HEX64 = re.compile(r"[0-9a-f]{64}")

STORE_HEAD_DOMAIN = "exomem-store-head:v1"


def _component(value: str, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    if "\0" in value:
        raise ValueError(f"{name} must not contain NUL")
    return value


def _count(value: int, name: str, *, minimum: int) -> str:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return str(value)


def _hex64(value: str, name: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        raise ValueError(f"{name} must be 64 lowercase hex characters")
    return value


def _digest(domain: str, *components: str) -> str:
    return hashlib.sha256("\0".join((domain, *components)).encode("utf-8")).hexdigest()


def store_head_hash(prev_store_head_hash: str | None, commit_seq: int, event_hash: str) -> str:
    """The store-wide chained head after one transaction (A3).

    ``sha256("exomem-store-head:v1\\0" + prev + "\\0" + commit_seq + "\\0" + event_hash)``.
    The first transaction of a store has no predecessor; it is encoded as the
    empty string, which no 64-hex head can equal.
    """
    prev = "" if prev_store_head_hash is None else _hex64(prev_store_head_hash, "prev head")
    return _digest(
        STORE_HEAD_DOMAIN,
        prev,
        _count(commit_seq, "commit_seq", minimum=1),
        _hex64(event_hash, "event_hash"),
    )
