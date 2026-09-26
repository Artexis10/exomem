"""An in-memory fake of B2 application-key management for tests.

Documents the real B2 semantics this fake stands in for: a B2 application
key created with `namePrefix` set can only list or delete file versions
whose name starts with that prefix. B2 enforces this server-side on every
call the key authenticates; a call against a name outside the key's prefix
is refused, not merely filtered. This fake refuses the same way, by raising
`PermissionError`, so a cellctl bug that reuses the wrong cell's key fails a
test instead of silently seeing an empty (and misleadingly "safe") result.
"""

from __future__ import annotations

import os
import uuid

from .interface import ObjectStorageKey, ObjectVersion


class FakeB2:
    def __init__(self, *, fixed_credentials: tuple[str, str] | None = None) -> None:
        self._keys: dict[str, str] = {}  # key_id -> name_prefix
        self._objects: dict[str, list[str]] = {}  # object key -> [version_id, ...]
        # Key management (create/delete/list/prefix-restriction) stays fully
        # faked; when a live S3-compatible test double (e.g. MinIO) is
        # standing in for B2 itself, this lets every created key actually
        # authenticate against it by returning that double's real
        # access-key id/secret instead of a random placeholder.
        self._fixed_credentials = fixed_credentials

    # -- test setup helpers, not part of the real ObjectStorage surface --

    def seed_object(self, key: str, *, version_id: str | None = None) -> str:
        version_id = version_id or uuid.uuid4().hex
        self._objects.setdefault(key, []).append(version_id)
        return version_id

    # -- ObjectStorage --

    def create_prefix_key(self, cell_id: str) -> ObjectStorageKey:
        prefix = f"cells/{cell_id}/"
        if self._fixed_credentials is not None:
            # All cells share one literal id/secret (the live S3 double's one
            # root credential), so a later create for a different cell_id
            # intentionally reuses the same dict entry rather than clobbering
            # a distinct one -- there is only ever one.
            key_id, secret = self._fixed_credentials
        else:
            key_id = f"fake-b2-key-{uuid.uuid4().hex[:12]}"
            secret = os.urandom(20).hex()
        self._keys[key_id] = prefix
        return ObjectStorageKey(key_id=key_id, key_secret=secret, name_prefix=prefix)

    def delete_key(self, key_id: str) -> None:
        self._keys.pop(key_id, None)

    def key_exists(self, key_id: str) -> bool:
        return key_id in self._keys

    def key_absent(self, key_id: str) -> bool:
        return key_id not in self._keys

    def _require_prefix(self, key_id: str, prefix: str) -> None:
        if key_id not in self._keys:
            raise PermissionError(f"unknown or deleted key {key_id!r}")
        allowed = self._keys[key_id]
        if not prefix.startswith(allowed):
            raise PermissionError(
                f"key {key_id!r} is restricted to {allowed!r}, refused for {prefix!r}"
            )

    def list_object_versions(self, prefix: str) -> list[ObjectVersion]:
        """Admin-scoped: this is cellctl's own D10 verification call, not a
        per-cell key's restricted view."""

        return [
            ObjectVersion(key=object_key, version_id=version_id)
            for object_key, versions in self._objects.items()
            if object_key.startswith(prefix)
            for version_id in versions
        ]

    def delete_object_version(self, version: ObjectVersion) -> None:
        versions = self._objects.get(version.key, [])
        if version.version_id in versions:
            versions.remove(version.version_id)
        if not versions:
            self._objects.pop(version.key, None)

    # -- test-only: a per-cell key's restricted view, documenting the real B2
    # prefix-restriction property that protects a backup/restore Job --

    def list_as_key(self, key_id: str, prefix: str) -> list[ObjectVersion]:
        self._require_prefix(key_id, prefix)
        return self.list_object_versions(prefix)

    def delete_as_key(self, key_id: str, version: ObjectVersion) -> None:
        self._require_prefix(key_id, version.key)
        self.delete_object_version(version)
