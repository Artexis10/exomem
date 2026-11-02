"""The key custodian's B2 client: the only code that creates or deletes an
application key (contain-cloud-controller-compromise D9). cellctl never
imports this module.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from .b2 import B2Session
from .interface import CELL_KEY_CAPABILITIES, ObjectStorageKey, cell_object_prefix

# b2_list_keys returns at most 1000 keys per page.
_LIST_PAGE_SIZE = 1000


@dataclass(frozen=True)
class B2KeyStoreConfig:
    key_id: str
    application_key: str
    bucket_id: str


class B2KeyStore(B2Session):
    def __init__(self, config: B2KeyStoreConfig, *, client: httpx.Client | None = None) -> None:
        super().__init__(config.key_id, config.application_key, client=client)
        self._config = config

    def create_prefix_key(self, cell_id: str) -> ObjectStorageKey:
        prefix = cell_object_prefix(cell_id)
        payload = self._post(
            "b2_create_key",
            {
                "accountId": self.account_id,
                "capabilities": list(CELL_KEY_CAPABILITIES),
                "keyName": f"cell-{cell_id}",
                "bucketIds": [self._config.bucket_id],
                "namePrefix": prefix,
            },
        )
        return ObjectStorageKey(
            key_id=payload["applicationKeyId"],
            key_secret=payload["applicationKey"],
            name_prefix=prefix,
        )

    def prefix_key_ids(self, cell_id: str) -> list[str]:
        prefix = cell_object_prefix(cell_id)
        request: dict = {"accountId": self.account_id, "maxKeyCount": _LIST_PAGE_SIZE}
        found: list[str] = []
        seen_cursors: set[str] = set()
        while True:
            payload = self._post("b2_list_keys", request)
            if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
                raise ValueError("malformed B2 key page")
            for entry in payload["keys"]:
                if not isinstance(entry, dict) or not isinstance(entry.get("applicationKeyId"), str):
                    raise ValueError("malformed B2 key entry")
                bucket_ids = entry.get("bucketIds")
                if bucket_ids is not None and not isinstance(bucket_ids, list):
                    raise ValueError("malformed B2 key entry")
                # v4 reports null for a key that spans every bucket; that key reaches this one too.
                reaches_bucket = bucket_ids is None or self._config.bucket_id in bucket_ids
                if entry.get("namePrefix") == prefix and reaches_bucket:
                    found.append(entry["applicationKeyId"])
            cursor = payload.get("nextApplicationKeyId")
            if cursor is None:
                return found
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise ValueError("malformed B2 key cursor")
            seen_cursors.add(cursor)
            request["startApplicationKeyId"] = cursor

    def delete_key(self, key_id: str) -> None:
        self._post("b2_delete_key", {"applicationKeyId": key_id})
