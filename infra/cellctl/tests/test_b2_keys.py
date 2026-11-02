"""The custodian's B2 client sends exactly the D9 key shape over API v4 and
reads every listed key, whatever its bucket restriction looks like."""

from __future__ import annotations

import json

import httpx
import pytest

from cellctl.storage.b2_keys import B2KeyStore, B2KeyStoreConfig
from cellctl.storage.interface import CELL_KEY_CAPABILITIES

CONFIG = B2KeyStoreConfig(key_id="custodian-key-id", application_key="custodian-app-key", bucket_id="backup-bucket")
CELL = "aaaaaaaaaaaaaaaa"


def _store(handler) -> B2KeyStore:
    def wrapped(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/b2api/v4/b2_authorize_account":
            return httpx.Response(
                200,
                json={
                    "accountId": "cell-data-account",
                    "apiInfo": {"storageApi": {"apiUrl": "https://api.example"}},
                    "authorizationToken": "tok",
                },
            )
        assert request.url.path.startswith("/b2api/v4/")
        return handler(request)

    return B2KeyStore(CONFIG, client=httpx.Client(transport=httpx.MockTransport(wrapped)))


def test_create_prefix_key_requests_the_exact_d9_shape() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("b2_create_key")
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"applicationKeyId": "new-key", "applicationKey": "new-secret"})

    key = _store(handler).create_prefix_key(CELL)

    assert bodies == [
        {
            "accountId": "cell-data-account",
            "capabilities": list(CELL_KEY_CAPABILITIES),
            "keyName": f"cell-{CELL}",
            "bucketIds": ["backup-bucket"],
            "namePrefix": f"cells/{CELL}/",
        }
    ]
    assert (key.key_id, key.key_secret, key.name_prefix) == ("new-key", "new-secret", f"cells/{CELL}/")


def test_prefix_key_ids_reads_every_page_and_only_this_cells_prefix_on_this_bucket() -> None:
    pages = [
        {
            "keys": [
                {"applicationKeyId": "other-cell", "namePrefix": "cells/bbbbbbbbbbbbbbbb/", "bucketIds": ["backup-bucket"]},
                {"applicationKeyId": "other-bucket", "namePrefix": f"cells/{CELL}/", "bucketIds": ["staging-bucket"]},
                {"applicationKeyId": "no-prefix", "namePrefix": None, "bucketIds": ["backup-bucket"]},
            ],
            "nextApplicationKeyId": "page-2",
        },
        {
            "keys": [
                {"applicationKeyId": "this-cell", "namePrefix": f"cells/{CELL}/", "bucketIds": ["backup-bucket"]},
                {"applicationKeyId": "this-cell-wide", "namePrefix": f"cells/{CELL}/", "bucketIds": None},
            ],
            "nextApplicationKeyId": None,
        },
    ]
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("b2_list_keys")
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=pages[len(requests) - 1])

    assert _store(handler).prefix_key_ids(CELL) == ["this-cell", "this-cell-wide"]
    assert requests[1]["startApplicationKeyId"] == "page-2"


def test_a_repeated_key_cursor_fails_instead_of_looping() -> None:
    page = {"keys": [], "nextApplicationKeyId": "same"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page)

    with pytest.raises(ValueError):
        _store(handler).prefix_key_ids(CELL)
