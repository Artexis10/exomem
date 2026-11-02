"""An in-memory fake of B2 object-version bookkeeping for tests.

It holds object versions only. Application keys belong to the key
custodian's fake (`fake_b2_keys.py`); cellctl never creates one.
"""

from __future__ import annotations

import uuid

from .interface import ObjectVersion


class FakeB2:
    def __init__(self) -> None:
        self._objects: dict[str, list[str]] = {}  # object key -> [version_id, ...]

    # -- test setup helper, not part of the real ObjectStorage surface --

    def seed_object(self, key: str, *, version_id: str | None = None) -> str:
        version_id = version_id or uuid.uuid4().hex
        self._objects.setdefault(key, []).append(version_id)
        return version_id

    # -- ObjectStorage --

    def list_object_versions(self, prefix: str) -> list[ObjectVersion]:
        return [
            ObjectVersion(key=object_key, version_id=version_id)
            for object_key, versions in self._objects.items()
            if object_key.startswith(prefix)
            for version_id in versions
        ]
