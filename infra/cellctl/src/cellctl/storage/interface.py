"""Protocols for the external services cellctl and the key custodian call (D7, D9, D10).

Real implementations live in b2.py, b2_keys.py and hetzner.py; test fakes live in
fake_b2.py, fake_b2_keys.py and fake_hetzner.py. cellctl's own code depends only on these
Protocols, never on the concrete client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# What a per-cell key must allow for restic to work through B2's S3 API.
# The key custodian mints exactly this set, and the key watcher pins it.
# listBuckets matters even on a bucket- and prefix-restricted key: without
# it, B2 answers a HEAD on a missing object with 403 instead of 404, and
# restic's first step (a HEAD on the repository's `config`) is exactly that.
# The key still sees only its own bucket and prefix.
CELL_KEY_CAPABILITIES: tuple[str, ...] = (
    "listBuckets",
    "listFiles",
    "readFiles",
    "writeFiles",
    "deleteFiles",
)


def cell_object_prefix(cell_id: str) -> str:
    """Where a cell's objects live in a bucket; also a per-cell key's `namePrefix`."""
    return f"cells/{cell_id}/"


@dataclass(frozen=True)
class ObjectStorageKey:
    key_id: str
    key_secret: str
    name_prefix: str


@dataclass(frozen=True)
class ObjectVersion:
    key: str
    version_id: str


class ObjectStorage(Protocol):
    """B2 object-version bookkeeping. Not the backup data path itself
    (restic does that, inside the Job), and no application-key operation:
    the key custodian alone creates and deletes keys (`PrefixKeyStore`).
    """

    def list_object_versions(self, prefix: str) -> list[ObjectVersion]:
        """Every version under `prefix`, including hidden ones (D10: "empty"
        means no versions at all, not just no visible ones).

        Called with cellctl's own bucket-restricted listing credential, not a
        per-cell key: cellctl is the trusted control plane. The per-cell
        key's prefix restriction is what protects a backup/restore Job, which
        authenticates as that key, not as cellctl.
        """
        ...

    def delete_object_version(self, version: ObjectVersion) -> None: ...


class PrefixKeyStore(Protocol):
    """The key custodian's B2 key operations, in the cell-data account only."""

    def create_prefix_key(self, cell_id: str) -> ObjectStorageKey:
        """A fresh application key restricted to the locked backup bucket
        and `cells/<cell_id>/`, with exactly `CELL_KEY_CAPABILITIES`."""
        ...

    def prefix_key_ids(self, cell_id: str) -> list[str]:
        """The IDs of every key the provider lists for `cells/<cell_id>/`
        on the backup bucket. The only source of an ID to delete."""
        ...

    def delete_key(self, key_id: str) -> None: ...


@dataclass(frozen=True)
class VolumeInfo:
    volume_id: str
    server_id: str
    labels: dict[str, str]
