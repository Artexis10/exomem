"""Domain-separated tokens for the structured-collection store (design §3, §16 A3).

Every token is a lowercase 64-hex SHA-256 digest over a domain tag and
NUL-separated components, so no two token kinds can collide and no component
can smuggle a separator. Integers are rendered in decimal ASCII.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

_HEX64 = re.compile(r"[0-9a-f]{64}")

GENERATION_DOMAIN = "exomem-collection-generation:v1"
ROW_DOMAIN = "exomem-collection-row:v1"
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



def container_hash(collection_id: str, generation: int, audit_head: str | None) -> str:
    """The collection guard: generation plus audit head (§3, wire ``*_container_hash``).

    A collection with no committed transaction has no audit head; it is
    encoded as the empty string. Legacy heads (24-hex transition ids) and
    store-native heads (64-hex event hashes) are both taken verbatim.
    """
    return _digest(
        GENERATION_DOMAIN,
        _component(collection_id, "collection_id"),
        _count(generation, "generation", minimum=0),
        "" if audit_head is None else _component(audit_head, "audit_head"),
    )


def item_version(collection_id: str, item_key: str, row_version: int, payload_hash: str) -> str:
    """The item guard: row version plus payload hash (§3, wire ``item_version``)."""
    return _digest(
        ROW_DOMAIN,
        _component(collection_id, "collection_id"),
        _component(item_key, "item_key"),
        _count(row_version, "row_version", minimum=1),
        _hex64(payload_hash, "payload_hash"),
    )


def manifest_hash(manifest_text: str) -> str:
    """``sha256`` of the exact manifest bytes, as the file manifest hash is today."""
    return hashlib.sha256(_component(manifest_text, "manifest_text").encode("utf-8")).hexdigest()


def _normalize_json(value: Any) -> Any:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {str(key): _normalize_json(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    return value


def payload_hash(
    schema_version: int, item_key: str, values: Mapping[str, Any], semantic_body: str
) -> str:
    """Content identity of one item version, byte-identical to file mode.

    This is ``records._payload_hash`` over the stored semantic body (the
    authored body with no managed presentation block): NFC strings, sorted
    object keys, compact UTF-8 JSON of
    ``[schema_version, item_key, [[name, value], ...], body]``.
    """
    ordered = [[name, _normalize_json(values[name])] for name in sorted(values)]
    payload = [schema_version, item_key, ordered, _normalize_json(semantic_body)]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()
