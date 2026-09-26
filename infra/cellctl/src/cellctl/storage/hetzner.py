"""Read-only Hetzner Cloud volume listing (D9, D10).

cellctl never creates, attaches or deletes a Hetzner volume directly — that
follows from PVC/PV lifecycle through the CSI driver. It only lists volumes,
to confirm deletion (D10) and to observe attachment counts (D9).
"""

from __future__ import annotations

import httpx

from .interface import VolumeInfo

_API_BASE = "https://api.hetzner.cloud/v1"


class HetznerVolumeProvider:
    def __init__(self, *, read_only_token: str, client: httpx.Client | None = None) -> None:
        self._token = read_only_token
        self._client = client or httpx.Client(timeout=30.0, base_url=_API_BASE)

    def _get(self, path: str, **params: object) -> dict:
        response = self._client.get(
            path, params=params, headers={"Authorization": f"Bearer {self._token}"}
        )
        response.raise_for_status()
        return response.json()

    def list_volumes(self) -> list[VolumeInfo]:
        volumes: list[VolumeInfo] = []
        page = 1
        while True:
            payload = self._get("/volumes", page=page, per_page=50)
            for entry in payload.get("volumes", []):
                server_id = str(entry["server"]) if entry.get("server") else ""
                volumes.append(
                    VolumeInfo(
                        volume_id=str(entry["id"]),
                        server_id=server_id,
                        labels=entry.get("labels", {}) or {},
                    )
                )
            meta = payload.get("meta", {}).get("pagination", {})
            if not meta.get("next_page"):
                break
            page = meta["next_page"]
        return volumes

    def get_volume(self, volume_id: str) -> VolumeInfo | None:
        for volume in self.list_volumes():
            if volume.volume_id == volume_id:
                return volume
        return None
