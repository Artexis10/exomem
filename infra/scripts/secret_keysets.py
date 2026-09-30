"""Exact field contracts for bundled SOPS Kubernetes Secrets."""

from __future__ import annotations

import json
import re
from typing import Any

MAX_SECRET_BYTES = 8192
_REDACTION_PLACEHOLDERS = frozenset({"[sensitive]", "<sensitive>", "[redacted]", "<redacted>"})
_SAFE_KEY = re.compile(r"[A-Za-z0-9._-]+\Z")


class KeySetError(ValueError):
    """A content-free bundle validation failure."""


def validate_key_sets(raw: Any) -> tuple[frozenset[str], ...]:
    if not isinstance(raw, list) or not raw:
        raise KeySetError("invalid key sets")
    sets: list[frozenset[str]] = []
    for keys in raw:
        if (
            not isinstance(keys, list)
            or not keys
            or any(not isinstance(key, str) or not _SAFE_KEY.fullmatch(key) for key in keys)
            or len(set(keys)) != len(keys)
        ):
            raise KeySetError("invalid key sets")
        key_set = frozenset(keys)
        if key_set in sets:
            raise KeySetError("invalid key sets")
        sets.append(key_set)
    return tuple(sets)


def validate_values(value: Any, key_sets: tuple[frozenset[str], ...]) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) not in key_sets:
        raise KeySetError("secret source has an invalid key set")
    for item in value.values():
        if (
            not isinstance(item, str)
            or not item
            or "\x00" in item
            or "\n" in item
            or "\r" in item
            or item.strip().casefold() in _REDACTION_PLACEHOLDERS
        ):
            raise KeySetError("secret source has an invalid value")
    try:
        encoded_size = len(
            json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
    except UnicodeEncodeError as exc:
        raise KeySetError("secret source has an invalid value") from exc
    if encoded_size > MAX_SECRET_BYTES:
        raise KeySetError("secret source has an invalid value")
    return value


def loads_unique_json(raw: bytes) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise KeySetError("secret source has duplicate keys")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except KeySetError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise KeySetError("secret source has invalid JSON") from exc


def parse_json_object(raw: bytes, key_sets: tuple[frozenset[str], ...]) -> dict[str, str]:
    if not raw or len(raw) > MAX_SECRET_BYTES or b"\x00" in raw:
        raise KeySetError("secret source has an invalid value")
    value = loads_unique_json(raw)
    return validate_values(value, key_sets)
