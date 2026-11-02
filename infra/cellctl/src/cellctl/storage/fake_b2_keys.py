"""An in-memory fake of the key custodian's B2 key operations for tests."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

from .interface import CELL_KEY_CAPABILITIES, ObjectStorageKey, cell_object_prefix


@dataclass(frozen=True)
class FakeKey:
    name_prefix: str
    capabilities: frozenset[str]
    bucket_ids: tuple[str, ...]


class FakeKeyStore:
    def __init__(self, *, bucket_id: str = "fake-bucket-id") -> None:
        self.bucket_id = bucket_id
        self.keys: dict[str, FakeKey] = {}

    def create_prefix_key(self, cell_id: str) -> ObjectStorageKey:
        prefix = cell_object_prefix(cell_id)
        key_id = f"fake-b2-key-{uuid.uuid4().hex[:12]}"
        self.keys[key_id] = FakeKey(
            name_prefix=prefix,
            capabilities=frozenset(CELL_KEY_CAPABILITIES),
            bucket_ids=(self.bucket_id,),
        )
        return ObjectStorageKey(key_id=key_id, key_secret=os.urandom(20).hex(), name_prefix=prefix)

    def prefix_key_ids(self, cell_id: str) -> list[str]:
        prefix = cell_object_prefix(cell_id)
        return [key_id for key_id, key in self.keys.items() if key.name_prefix == prefix]

    def delete_key(self, key_id: str) -> None:
        self.keys.pop(key_id, None)
