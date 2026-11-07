"""An in-memory fake of B2 object-version bookkeeping for tests.

It holds object versions only. Application keys belong to the key
custodian's fake (`fake_b2_keys.py`); cellctl never creates one.
"""

from __future__ import annotations

import uuid

from .interface import ObjectVersion


class FakeB2:
    def __init__(self) -> None:
        # object key -> [(version_id, size, action), ...]
        self._objects: dict[str, list[tuple[str, int | None, str | None]]] = {}
        self.listed: list[str] = []
        # A listing that fails as the real client does on a bad or missing page.
        self.fail_listing_with: Exception | None = None

    # -- test setup helper, not part of the real ObjectStorage surface --

    def seed_object(
        self, key: str, *, version_id: str | None = None, size: int | None = None, action: str | None = None,
    ) -> str:
        version_id = version_id or uuid.uuid4().hex
        self._objects.setdefault(key, []).append((version_id, size, action))
        return version_id

    # -- ObjectStorage --

    def list_object_versions(self, prefix: str) -> list[ObjectVersion]:
        self.listed.append(prefix)
        if self.fail_listing_with is not None:
            raise self.fail_listing_with
        return [
            ObjectVersion(key=object_key, version_id=version_id, size=size, action=action)
            for object_key, versions in self._objects.items()
            if object_key.startswith(prefix)
            for version_id, size, action in versions
        ]
