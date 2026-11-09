"""The short id of a page: its shape, how it is minted, and how a guard compares one.

A short id is `<7 base32 of exomem_id>.<4 base32 of the content hash>`. The head names the page and
survives edits and moves. The tail names one version of its bytes. A page without `exomem_id` takes a
hash of its path as its head, which goes stale when the page moves.

This module holds only the format, so that the leaf writers (`edit`) and the resolver
(`page_names`) share one spelling without importing each other.
"""

from __future__ import annotations

import base64
import hashlib
import re
import uuid

# A format check on a value this module itself mints (fixed-width RFC 4648 base32 alphabet), not a
# reading of what a caller meant, so it is sound under registry C6.
_ID_PATTERN = re.compile(r"[a-z2-7]{7}\.[a-z2-7]{4}")
HEAD_CHARS = 7
VERSION_CHARS = 4


def is_page_id(value: object) -> bool:
    """Whether `value` has the shape of a short page id."""
    return isinstance(value, str) and _ID_PATTERN.fullmatch(value) is not None


def _base32(raw: bytes, chars: int) -> str:
    return base64.b32encode(raw).decode("ascii").lower()[:chars]


def id_head(exomem_id: str | None, rel_path: str) -> str:
    """7 base32 characters of `exomem_id`; a page without one takes its path hash."""
    if exomem_id:
        return _base32(uuid.UUID(exomem_id).bytes[:5], HEAD_CHARS)
    return _base32(hashlib.sha256(rel_path.encode("utf-8")).digest()[:5], HEAD_CHARS)


def id_version(content_hash: str) -> str:
    """4 base32 characters of the page's content hash."""
    return _base32(bytes.fromhex(content_hash)[:3], VERSION_CHARS)


def page_id(exomem_id: str | None, rel_path: str, content_hash: str) -> str:
    return f"{id_head(exomem_id, rel_path)}.{id_version(content_hash)}"


def guard_matches(expected: str, content_hash: str) -> bool:
    """Whether an `expected_hash` names the version whose full hash is `content_hash`.

    The guard is either that full hash or a short id; an id names the version by its tail, and the
    path the caller gave names the page, so the head is not compared.
    """
    if is_page_id(expected):
        return expected.partition(".")[2] == id_version(content_hash)
    return expected == content_hash
