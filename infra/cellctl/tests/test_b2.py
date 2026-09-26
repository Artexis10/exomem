"""Tests for the real B2 native-API client (H8/SR-H2): the cached
authorization must be refreshed on a 401, not held forever."""

from __future__ import annotations

import json

import httpx

from cellctl.storage.b2 import B2Config, B2ObjectStorage

CONFIG = B2Config(
    key_management_key_id="km-key-id",
    key_management_application_key="km-app-key",
    bucket_id="bucket-1",
    account_id="account-1",
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_create_prefix_key_requests_the_exact_bucket_and_cell_prefix() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("b2_authorize_account"):
            return httpx.Response(
                200,
                json={"apiInfo": {"storageApi": {"apiUrl": "https://api.example"}}, "authorizationToken": "tok"},
            )
        assert request.url.path.endswith("b2_create_key")
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"applicationKeyId": "cell-key", "applicationKey": "secret"})

    key = B2ObjectStorage(CONFIG, client=_client(handler)).create_prefix_key("aaaaaaaaaaaaaaaa")

    assert len(requests) == 1
    assert requests[0]["bucketId"] == CONFIG.bucket_id
    assert requests[0]["namePrefix"] == "cells/aaaaaaaaaaaaaaaa/"
    assert key.name_prefix == requests[0]["namePrefix"]


def test_authorize_is_cached_across_calls() -> None:
    calls = {"authorize": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("b2_authorize_account"):
            calls["authorize"] += 1
            return httpx.Response(
                200,
                json={"apiInfo": {"storageApi": {"apiUrl": "https://api.example"}}, "authorizationToken": "tok-1"},
            )
        return httpx.Response(200, json={"keys": []})

    storage = B2ObjectStorage(CONFIG, client=_client(handler))
    storage.key_absent("key-1")
    storage.key_absent("key-2")
    assert calls["authorize"] == 1


def test_a_401_clears_the_cache_and_retries_once() -> None:
    """H8/SR-H2: B2 authorization tokens expire after 24h. Without a
    refresh-on-401, every call after that failed forever -- starving new
    cells (create_prefix_key) and stuck deletions (list_object_versions/
    key_absent) alike, and compounding H4 (one row wedging the whole pass)."""

    calls = {"authorize": 0, "post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("b2_authorize_account"):
            calls["authorize"] += 1
            token = "expired-token" if calls["authorize"] == 1 else "fresh-token"
            return httpx.Response(
                200,
                json={"apiInfo": {"storageApi": {"apiUrl": "https://api.example"}}, "authorizationToken": token},
            )
        calls["post"] += 1
        if request.headers.get("Authorization") == "expired-token":
            return httpx.Response(401, json={"code": "expired_auth_token", "message": "expired"})
        return httpx.Response(200, json={"keys": []})

    storage = B2ObjectStorage(CONFIG, client=_client(handler))
    assert storage.key_absent("key-1") is True
    assert calls["authorize"] == 2  # the initial authorize, then the 401-triggered re-authorize
    assert calls["post"] == 2  # the failed call, then the retry that succeeded

    # A subsequent call reuses the now-fresh cached token, not a third authorize.
    storage.key_absent("key-2")
    assert calls["authorize"] == 2


def test_two_consecutive_401s_are_not_retried_forever() -> None:
    # The retry is "once", not a loop -- a persistently bad credential must
    # still surface as an error rather than spin.
    calls = {"authorize": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("b2_authorize_account"):
            calls["authorize"] += 1
            return httpx.Response(
                200,
                json={"apiInfo": {"storageApi": {"apiUrl": "https://api.example"}}, "authorizationToken": "tok"},
            )
        return httpx.Response(401, json={"code": "expired_auth_token", "message": "expired"})

    storage = B2ObjectStorage(CONFIG, client=_client(handler))
    try:
        storage.key_absent("key-1")
        raised = False
    except httpx.HTTPStatusError:
        raised = True
    assert raised is True
    assert calls["authorize"] == 2  # initial + the one retry, never more
