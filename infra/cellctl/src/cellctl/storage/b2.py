"""Real B2 native-API client: application-key management and object-version
bookkeeping only. The backup data path itself is restic, inside the Job.

KNOWN LIMITATION: list_object_versions reads a single page (up to
`page_size` versions). A cell's backup prefix is small (7 daily + 4 weekly
restic snapshots' worth of pack files), so this is expected to be enough in
practice, but D10 deletion should loop on `nextFileName`/`nextFileId` before
this is trusted at scale. Flagged rather than silently accepted.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .interface import ObjectStorageKey, ObjectVersion

_AUTHORIZE_URL = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
_KEY_CAPABILITIES = ["listFiles", "readFiles", "writeFiles", "deleteFiles"]


@dataclass(frozen=True)
class B2Config:
    key_management_key_id: str
    key_management_application_key: str
    bucket_id: str
    account_id: str
    page_size: int = 10_000


class B2ObjectStorage:
    def __init__(self, config: B2Config, *, client: httpx.Client | None = None) -> None:
        self._config = config
        self._client = client or httpx.Client(timeout=30.0)
        self._api_url: str | None = None
        self._auth_token: str | None = None

    def _authorize(self) -> tuple[str, str]:
        if self._api_url and self._auth_token:
            return self._api_url, self._auth_token
        response = self._client.get(
            _AUTHORIZE_URL,
            auth=(self._config.key_management_key_id, self._config.key_management_application_key),
        )
        response.raise_for_status()
        payload = response.json()
        self._api_url = payload["apiInfo"]["storageApi"]["apiUrl"]
        self._auth_token = payload["authorizationToken"]
        return self._api_url, self._auth_token

    def _post(self, endpoint: str, body: dict) -> dict:
        api_url, token = self._authorize()
        response = self._client.post(
            f"{api_url}/b2api/v3/{endpoint}", json=body, headers={"Authorization": token}
        )
        if response.status_code == 401:
            # D7/H8: B2 authorization expires after 24h. On a 401, discard
            # the cached authorization, re-authorize, and retry the call
            # once -- otherwise every call fails forever after a day of
            # uptime, which starves new cells and stuck deletions alike.
            self._api_url = None
            self._auth_token = None
            api_url, token = self._authorize()
            response = self._client.post(
                f"{api_url}/b2api/v3/{endpoint}", json=body, headers={"Authorization": token}
            )
        response.raise_for_status()
        return response.json()

    def create_prefix_key(self, cell_id: str) -> ObjectStorageKey:
        prefix = f"cells/{cell_id}/"
        payload = self._post(
            "b2_create_key",
            {
                "accountId": self._config.account_id,
                "capabilities": _KEY_CAPABILITIES,
                "keyName": f"cell-{cell_id}",
                "bucketId": self._config.bucket_id,
                "namePrefix": prefix,
            },
        )
        return ObjectStorageKey(
            key_id=payload["applicationKeyId"],
            key_secret=payload["applicationKey"],
            name_prefix=prefix,
        )

    def delete_key(self, key_id: str) -> None:
        self._post("b2_delete_key", {"applicationKeyId": key_id})

    def key_absent(self, key_id: str) -> bool:
        payload = self._post(
            "b2_list_keys",
            {"accountId": self._config.account_id, "startApplicationKeyId": key_id, "maxKeyCount": 1},
        )
        keys = payload.get("keys", [])
        return not any(entry["applicationKeyId"] == key_id for entry in keys)

    def list_object_versions(self, prefix: str) -> list[ObjectVersion]:
        payload = self._post(
            "b2_list_file_versions",
            {
                "bucketId": self._config.bucket_id,
                "prefix": prefix,
                "maxFileCount": self._config.page_size,
            },
        )
        return [
            ObjectVersion(key=entry["fileName"], version_id=entry["fileId"])
            for entry in payload.get("files", [])
        ]

    def delete_object_version(self, version: ObjectVersion) -> None:
        self._post(
            "b2_delete_file_version", {"fileName": version.key, "fileId": version.version_id}
        )
