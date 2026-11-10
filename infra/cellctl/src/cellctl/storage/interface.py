"""Protocols for the external services cellctl and the key custodian call (D7, D9, D10).

Real implementations live in b2.py, b2_keys.py and hetzner.py; test fakes live in
fake_b2.py, fake_b2_keys.py and fake_hetzner.py. cellctl's own code depends only on these
Protocols, never on the concrete client.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
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

# automate-cell-storage-deletion D2: the key custodian's short-lived purge key may list and hide in one cell's
# prefix of the locked backup bucket, and nothing else. The key watcher pins the same set as the purge shape.
PURGE_KEY_CAPABILITIES: tuple[str, ...] = ("listFiles", "writeFiles")
# B2's documented file-version actions that this code reads: a protocol's closed set, not data.
UPLOAD_ACTION = "upload"
START_ACTION = "start"
HIDE_ACTION = "hide"


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
    """One listed version. `size` and `action` are what the provider reported, and None when it
    reported none: the physical meter (media_meter.py) treats a missing size as unknown, never as 0."""

    key: str
    version_id: str
    size: int | None = None
    action: str | None = None


def current_names(versions: Iterable[ObjectVersion]) -> list[str]:
    """The names whose newest entry, apart from `start` entries, is an `upload` (D4). `versions` is in the
    provider's order: by name, and newest first within a name."""

    current: list[str] = []
    decided: str | None = None
    for version in versions:
        if version.key == decided or version.action == START_ACTION:
            continue
        decided = version.key
        if version.action == UPLOAD_ACTION:
            current.append(version.key)
    return current


def holds_stored_entry(versions: Iterable[ObjectVersion]) -> bool:
    """Whether a listing shows any entry but a hide marker, such as an `upload` or a `start` (D1). An entry
    whose action is unknown counts too: only a listing of hide markers alone proves the data is gone."""

    return any(version.action != HIDE_ACTION for version in versions)


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

    def iter_object_versions(self, prefix: str) -> Iterator[ObjectVersion]:
        """`list_object_versions` one page at a time, for a caller that only sums: it holds one page
        in memory, not the whole prefix. A failed page raises after the earlier pages were yielded."""
        ...


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

    def create_purge_key(self, cell_id: str, valid_seconds: int) -> ObjectStorageKey:
        """A key restricted to the locked backup bucket and `cells/<cell_id>/`, with exactly
        `PURGE_KEY_CAPABILITIES`, that expires after `valid_seconds` (automate-cell-storage-deletion D2)."""
        ...

    def delete_key(self, key_id: str) -> None: ...


class PrefixHider(Protocol):
    """What the custodian does with a purge key: list one prefix's versions and hide a name."""

    def iter_object_versions(self, prefix: str) -> Iterator[ObjectVersion]: ...

    def hide_file(self, name: str) -> None: ...


@dataclass(frozen=True)
class VolumeInfo:
    volume_id: str
    server_id: str
    labels: dict[str, str]
