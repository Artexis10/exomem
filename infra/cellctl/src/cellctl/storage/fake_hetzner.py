"""An in-memory fake of the read-only Hetzner volume listing, for tests."""

from __future__ import annotations

from .interface import VolumeInfo


class FakeHetznerVolumeProvider:
    def __init__(self, volumes: list[VolumeInfo] | None = None) -> None:
        self._volumes = {v.volume_id: v for v in (volumes or [])}

    def add_volume(self, volume: VolumeInfo) -> None:
        self._volumes[volume.volume_id] = volume

    def remove_volume(self, volume_id: str) -> None:
        self._volumes.pop(volume_id, None)

    def list_volumes(self) -> list[VolumeInfo]:
        return list(self._volumes.values())

    def get_volume(self, volume_id: str) -> VolumeInfo | None:
        return self._volumes.get(volume_id)
