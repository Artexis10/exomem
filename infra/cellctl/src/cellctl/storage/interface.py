"""Protocols for the two external services cellctl itself calls (D7, D9, D10).

Real implementations live in b2.py and hetzner.py; test fakes live in
fake_b2.py and fake_hetzner.py. cellctl's own code depends only on these
Protocols, never on the concrete client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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
    """B2 application-key management and object-version bookkeeping.

    Not the backup data path itself (restic does that, inside the Job).
    """

    def create_prefix_key(self, cell_id: str) -> ObjectStorageKey:
        """A fresh application key restricted to `cells/<cell_id>/`."""
        ...

    def delete_key(self, key_id: str) -> None: ...

    def key_absent(self, key_id: str) -> bool:
        """Proof of absence for D10: checked via the key-management
        credential's own key listing, not via the (possibly already
        deleted) per-cell key's own auth."""
        ...

    def list_object_versions(self, prefix: str) -> list[ObjectVersion]:
        """Every version under `prefix`, including hidden ones (D10: "empty"
        means no versions at all, not just no visible ones).

        Called with cellctl's own admin key-management credential, not a
        per-cell key: cellctl is the trusted control plane and needs this to
        keep working for D10 verification even after a cell's own key is
        deleted. The per-cell key's prefix restriction is what protects a
        backup/restore Job, which authenticates as that key, not as cellctl.
        """
        ...

    def delete_object_version(self, version: ObjectVersion) -> None: ...


@dataclass(frozen=True)
class VolumeInfo:
    volume_id: str
    server_id: str
    labels: dict[str, str]


class VolumeProvider(Protocol):
    """Read-only Hetzner volume listing (D9, D10). cellctl never creates,
    attaches or deletes a volume directly; that follows from PVC/PV
    lifecycle through the CSI driver."""

    def list_volumes(self) -> list[VolumeInfo]: ...

    def get_volume(self, volume_id: str) -> VolumeInfo | None: ...
