"""Real B2 native-API client: object-version bookkeeping only. The backup
data path itself is restic, inside the Job. Application keys are created by
the key custodian alone (`b2_keys.py`); nothing here can create, list or
delete one.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .interface import ObjectVersion

# v4 reports a key's bucket restriction as the `bucketIds` list; v3 hid keys that
# carry more than one bucket from the listing (contain-cloud-controller-compromise D9).
_API_VERSION = "b2api/v4"
_AUTHORIZE_URL = f"https://api.backblazeb2.com/{_API_VERSION}/b2_authorize_account"


@dataclass(frozen=True)
class B2Config:
    key_id: str
    application_key: str
    bucket_id: str
    page_size: int = 10_000


class B2Session:
    """One authorized B2 native-API session, refreshed once on a 401."""

    def __init__(
        self, key_id: str, application_key: str, *, client: httpx.Client | None = None
    ) -> None:
        self._key_id = key_id
        self._application_key = application_key
        self._client = client or httpx.Client(timeout=30.0)
        self._api_url: str | None = None
        self._auth_token: str | None = None
        self._account_id: str | None = None

    def _authorize(self) -> tuple[str, str]:
        if self._api_url and self._auth_token:
            return self._api_url, self._auth_token
        response = self._client.get(_AUTHORIZE_URL, auth=(self._key_id, self._application_key))
        response.raise_for_status()
        payload = response.json()
        self._api_url = payload["apiInfo"]["storageApi"]["apiUrl"]
        self._auth_token = payload["authorizationToken"]
        self._account_id = payload["accountId"]
        return self._api_url, self._auth_token

    @property
    def account_id(self) -> str:
        """The account the authorized key belongs to, as the provider reports it."""
        self._authorize()
        assert self._account_id is not None
        return self._account_id

    def _post(self, endpoint: str, body: dict) -> dict:
        api_url, token = self._authorize()
        response = self._client.post(
            f"{api_url}/{_API_VERSION}/{endpoint}", json=body, headers={"Authorization": token}
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
                f"{api_url}/{_API_VERSION}/{endpoint}", json=body, headers={"Authorization": token}
            )
        response.raise_for_status()
        return response.json()


class B2ObjectStorage(B2Session):
    def __init__(self, config: B2Config, *, client: httpx.Client | None = None) -> None:
        super().__init__(config.key_id, config.application_key, client=client)
        self._config = config

    def list_object_versions(self, prefix: str) -> list[ObjectVersion]:
        request = {
            "bucketId": self._config.bucket_id,
            "prefix": prefix,
            "maxFileCount": self._config.page_size,
        }
        versions: list[ObjectVersion] = []
        seen_cursors: set[tuple[str, str]] = set()
        while True:
            payload = self._post("b2_list_file_versions", request)
            if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
                raise ValueError("malformed B2 file version page")
            for entry in payload["files"]:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("fileName"), str)
                    or not entry["fileName"].startswith(prefix)
                    or not isinstance(entry.get("fileId"), str)
                    or not entry["fileId"]
                ):
                    raise ValueError("malformed B2 file version entry")
                versions.append(ObjectVersion(key=entry["fileName"], version_id=entry["fileId"]))

            if "nextFileName" not in payload or "nextFileId" not in payload:
                raise ValueError("missing B2 file version cursor")
            next_name, next_id = payload["nextFileName"], payload["nextFileId"]
            if next_name is None and next_id is None:
                return versions
            if (
                not isinstance(next_name, str)
                or not next_name
                or not isinstance(next_id, str)
                or not next_id
                or not payload["files"]
            ):
                raise ValueError("malformed B2 file version cursor")
            cursor = (next_name, next_id)
            if cursor in seen_cursors:
                raise ValueError("repeated B2 file version cursor")
            seen_cursors.add(cursor)
            request["startFileName"], request["startFileId"] = cursor
