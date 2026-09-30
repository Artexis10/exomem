"""The real B2 client must list every version before D10 can confirm deletion."""

from __future__ import annotations

import json

import httpx
import pytest

from cellctl.storage.b2 import B2Config, B2ObjectStorage
from cellctl.storage.interface import ObjectVersion

PREFIX = "cells/aaaaaaaaaaaaaaaa/"
CONFIG = B2Config(
    key_management_key_id="km-key-id",
    key_management_application_key="km-app-key",
    bucket_id="bucket-1",
    account_id="account-1",
    page_size=2,
)


def _storage(responses: list[httpx.Response]) -> tuple[B2ObjectStorage, list[dict]]:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("b2_authorize_account"):
            return httpx.Response(
                200,
                json={
                    "apiInfo": {"storageApi": {"apiUrl": "https://api.example"}},
                    "authorizationToken": "tok",
                },
            )
        assert request.url.path.endswith("b2_list_file_versions")
        requests.append(json.loads(request.content))
        return responses[len(requests) - 1]

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return B2ObjectStorage(CONFIG, client=client), requests


def test_lists_all_pages_including_hide_markers_with_the_same_bucket_and_prefix() -> None:
    storage, requests = _storage([
        httpx.Response(200, json={
            "files": [
                {"fileName": PREFIX + "snapshot", "fileId": "upload-2", "action": "upload"},
                {"fileName": PREFIX + "snapshot", "fileId": "hide-1", "action": "hide"},
            ],
            "nextFileName": PREFIX + "snapshot", "nextFileId": "upload-1",
        }),
        httpx.Response(200, json={
            "files": [{"fileName": PREFIX + "snapshot", "fileId": "upload-1", "action": "upload"}],
            "nextFileName": None, "nextFileId": None,
        }),
    ])

    assert storage.list_object_versions(PREFIX) == [
        ObjectVersion(key=PREFIX + "snapshot", version_id="upload-2"),
        ObjectVersion(key=PREFIX + "snapshot", version_id="hide-1"),
        ObjectVersion(key=PREFIX + "snapshot", version_id="upload-1"),
    ]
    assert requests == [
        {"bucketId": "bucket-1", "prefix": PREFIX, "maxFileCount": 2},
        {"bucketId": "bucket-1", "prefix": PREFIX, "maxFileCount": 2,
         "startFileName": PREFIX + "snapshot", "startFileId": "upload-1"},
    ]


def test_empty_terminal_page_finishes_the_listing() -> None:
    storage, requests = _storage([
        httpx.Response(200, json={
            "files": [{"fileName": PREFIX + "snapshot", "fileId": "upload-1"}],
            "nextFileName": PREFIX + "later", "nextFileId": "later-1",
        }),
        httpx.Response(200, json={"files": [], "nextFileName": None, "nextFileId": None}),
    ])

    assert storage.list_object_versions(PREFIX) == [
        ObjectVersion(key=PREFIX + "snapshot", version_id="upload-1"),
    ]
    assert len(requests) == 2


@pytest.mark.parametrize("last_page", [
    {"files": [], "nextFileName": None},
    {"files": [], "nextFileName": PREFIX + "later", "nextFileId": None},
    {"files": [], "nextFileName": PREFIX + "later", "nextFileId": "later-1"},
    {"files": "not-a-list", "nextFileName": None, "nextFileId": None},
    {"files": [{"fileName": PREFIX + "later"}], "nextFileName": None, "nextFileId": None},
    {"files": [{"fileName": "cells/other/snapshot", "fileId": "other-1"}],
     "nextFileName": None, "nextFileId": None},
])
def test_malformed_later_page_never_returns_a_partial_listing(last_page: dict) -> None:
    storage, _ = _storage([
        httpx.Response(200, json={
            "files": [{"fileName": PREFIX + "snapshot", "fileId": "upload-1"}],
            "nextFileName": PREFIX + "later", "nextFileId": "later-1",
        }),
        httpx.Response(200, json=last_page),
    ])

    with pytest.raises(ValueError):
        storage.list_object_versions(PREFIX)


def test_repeated_cursor_fails_instead_of_looping_or_returning_a_partial_listing() -> None:
    cursor = {"nextFileName": PREFIX + "later", "nextFileId": "later-1"}
    storage, requests = _storage([
        httpx.Response(200, json={
            "files": [{"fileName": PREFIX + "snapshot", "fileId": "upload-1"}], **cursor,
        }),
        httpx.Response(200, json={
            "files": [{"fileName": PREFIX + "later", "fileId": "later-1"}], **cursor,
        }),
    ])

    with pytest.raises(ValueError):
        storage.list_object_versions(PREFIX)
    assert len(requests) == 2


def test_provider_error_after_a_page_never_returns_a_partial_listing() -> None:
    storage, requests = _storage([
        httpx.Response(200, json={
            "files": [{"fileName": PREFIX + "snapshot", "fileId": "upload-1"}],
            "nextFileName": PREFIX + "later", "nextFileId": "later-1",
        }),
        httpx.Response(503, json={"code": "service_unavailable"}),
    ])

    with pytest.raises(httpx.HTTPStatusError):
        storage.list_object_versions(PREFIX)
    assert len(requests) == 2
