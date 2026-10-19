"""Exact enum interning; dictionary IDs never define comparison order."""

import math

from ..query_engine.scalars import scalar_key
from .native_metadata import FieldMetadata
from .typed_storage import TypedStorageError


def seed(values, metadata: FieldMetadata) -> None:
    """Keep declared order without collapsing equality-equivalent representations."""
    for value in values:
        metadata.intern(value)


def encode(value: object, metadata: FieldMetadata) -> int | None:
    """Intern after schema validation; newly accepted representations append once."""
    return None if value is None else metadata.intern(value)


def decode(value_id: object, metadata: FieldMetadata, *, max_bytes: int) -> str | int | float | bool | None:
    if value_id is None:
        return None
    if type(value_id) is not int or value_id < 1:
        raise TypedStorageError("native enum identity is invalid")
    value = metadata.format(value_id, max_bytes=max_bytes)
    if type(value) not in (str, int, float, bool) or (type(value) is float and not math.isfinite(value)):
        raise TypedStorageError("native enum value is invalid")
    return value


def operand(value_id: object, metadata: FieldMetadata, *, max_bytes: int) -> tuple[int, str]:
    """Reuse logical comparison keys; never compare dictionary IDs directly."""
    return scalar_key(decode(value_id, metadata, max_bytes=max_bytes), "enum")
