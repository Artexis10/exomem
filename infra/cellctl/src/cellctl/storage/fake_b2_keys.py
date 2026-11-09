"""An in-memory fake of the key custodian's B2 key operations for tests."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

from .interface import (
    CELL_KEY_CAPABILITIES,
    PURGE_KEY_CAPABILITIES,
    ObjectStorageKey,
    ObjectVersion,
    cell_object_prefix,
)


@dataclass(frozen=True)
class FakeKey:
    name_prefix: str
    capabilities: frozenset[str]
    bucket_ids: tuple[str, ...]
    valid_seconds: int | None = None


class FakeKeyStore:
    def __init__(self, *, bucket_id: str = "fake-bucket-id") -> None:
        self.bucket_id = bucket_id
        self.keys: dict[str, FakeKey] = {}
        # Every key ever created, in order, so a test can see a mint that a later delete hid.
        self.created: list[FakeKey] = []

    def _create(self, cell_id: str, capabilities: tuple[str, ...], valid_seconds: int | None) -> ObjectStorageKey:
        prefix = cell_object_prefix(cell_id)
        key_id = f"fake-b2-key-{uuid.uuid4().hex[:12]}"
        key = FakeKey(name_prefix=prefix, capabilities=frozenset(capabilities), bucket_ids=(self.bucket_id,),
                      valid_seconds=valid_seconds)
        self.keys[key_id] = key
        self.created.append(key)
        return ObjectStorageKey(key_id=key_id, key_secret=os.urandom(20).hex(), name_prefix=prefix)

    def create_prefix_key(self, cell_id: str) -> ObjectStorageKey:
        return self._create(cell_id, CELL_KEY_CAPABILITIES, None)

    def create_purge_key(self, cell_id: str, valid_seconds: int) -> ObjectStorageKey:
        return self._create(cell_id, PURGE_KEY_CAPABILITIES, valid_seconds)

    def prefix_key_ids(self, cell_id: str) -> list[str]:
        prefix = cell_object_prefix(cell_id)
        return [key_id for key_id, key in self.keys.items() if key.name_prefix == prefix]

    def delete_key(self, key_id: str) -> None:
        self.keys.pop(key_id, None)


class FakeBucket:
    """A bucket's versions by name, newest first, as the provider lists them. Only a live key reaches it,
    and only inside the key's prefix and capabilities."""

    def __init__(self, keys: FakeKeyStore) -> None:
        self.key_store = keys
        self.actions: dict[str, list[str]] = {}
        self.hidden: list[str] = []

    def seed(self, name: str, *newest_first: str) -> None:
        self.actions[name] = list(newest_first)

    def hider(self, key: ObjectStorageKey) -> FakeHider:
        return FakeHider(self, key)


class FakeHider:
    def __init__(self, bucket: FakeBucket, key: ObjectStorageKey) -> None:
        self._bucket = bucket
        self._key_id = key.key_id

    def _require(self, capability: str, name: str) -> None:
        key = self._bucket.key_store.keys.get(self._key_id)
        if key is None or capability not in key.capabilities or not name.startswith(key.name_prefix):
            raise PermissionError(f"the key may not {capability} {name}")

    def iter_object_versions(self, prefix: str) -> Iterator[ObjectVersion]:
        self._require("listFiles", prefix)
        for name in sorted(self._bucket.actions):
            if name.startswith(prefix):
                for index, action in enumerate(self._bucket.actions[name]):
                    yield ObjectVersion(key=name, version_id=f"{name}#{index}", action=action)

    def hide_file(self, name: str) -> None:
        self._require("writeFiles", name)
        self._bucket.actions.setdefault(name, []).insert(0, "hide")
        self._bucket.hidden.append(name)
