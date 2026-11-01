"""Tests for the real B2 native-API client (H8/SR-H2): the cached
authorization must be refreshed on a 401, not held forever, and every call
goes through API v4."""

from __future__ import annotations

import httpx

from cellctl.storage.b2 import B2Config, B2ObjectStorage
from cellctl.storage.interface import ObjectVersion

CONFIG = B2Config(key_id="list-key-id", application_key="list-app-key", bucket_id="bucket-1")


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _authorized(token: str = "tok") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "accountId": "account-1",
            "apiInfo": {"storageApi": {"apiUrl": "https://api.example"}},
            "authorizationToken": token,
        },
    )


_EMPTY_PAGE = {"files": [], "nextFileName": None, "nextFileId": None}


def test_every_call_goes_through_api_v4() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("b2_authorize_account"):
            return _authorized()
        return httpx.Response(200, json=_EMPTY_PAGE)

    B2ObjectStorage(CONFIG, client=_client(handler)).list_object_versions("cells/aaaaaaaaaaaaaaaa/")

    assert paths == ["/b2api/v4/b2_authorize_account", "/b2api/v4/b2_list_file_versions"]


def test_delete_object_version_names_the_exact_file_and_id() -> None:
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("b2_authorize_account"):
            return _authorized()
        assert request.url.path.endswith("b2_delete_file_version")
        bodies.append(request.content)
        return httpx.Response(200, json={})

    B2ObjectStorage(CONFIG, client=_client(handler)).delete_object_version(
        ObjectVersion(key="cells/aaaaaaaaaaaaaaaa/pack", version_id="file-1")
    )

    assert bodies == [b'{"fileName":"cells/aaaaaaaaaaaaaaaa/pack","fileId":"file-1"}']


def test_authorize_is_cached_across_calls() -> None:
    calls = {"authorize": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("b2_authorize_account"):
            calls["authorize"] += 1
            return _authorized("tok-1")
        return httpx.Response(200, json=_EMPTY_PAGE)

    storage = B2ObjectStorage(CONFIG, client=_client(handler))
    storage.list_object_versions("cells/aaaaaaaaaaaaaaaa/")
    storage.list_object_versions("cells/bbbbbbbbbbbbbbbb/")
    assert calls["authorize"] == 1


def test_a_401_clears_the_cache_and_retries_once() -> None:
    """H8/SR-H2: B2 authorization tokens expire after 24h. Without a
    refresh-on-401, every call after that failed forever, starving stuck
    deletions and, in the key custodian, every new cell."""

    calls = {"authorize": 0, "post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("b2_authorize_account"):
            calls["authorize"] += 1
            return _authorized("expired-token" if calls["authorize"] == 1 else "fresh-token")
        calls["post"] += 1
        if request.headers.get("Authorization") == "expired-token":
            return httpx.Response(401, json={"code": "expired_auth_token", "message": "expired"})
        return httpx.Response(200, json=_EMPTY_PAGE)

    storage = B2ObjectStorage(CONFIG, client=_client(handler))
    assert storage.list_object_versions("cells/aaaaaaaaaaaaaaaa/") == []
    assert calls["authorize"] == 2  # the initial authorize, then the 401-triggered re-authorize
    assert calls["post"] == 2  # the failed call, then the retry that succeeded

    # A subsequent call reuses the now-fresh cached token, not a third authorize.
    storage.list_object_versions("cells/bbbbbbbbbbbbbbbb/")
    assert calls["authorize"] == 2


def test_two_consecutive_401s_are_not_retried_forever() -> None:
    # The retry is "once", not a loop -- a persistently bad credential must
    # still surface as an error rather than spin.
    calls = {"authorize": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("b2_authorize_account"):
            calls["authorize"] += 1
            return _authorized()
        return httpx.Response(401, json={"code": "expired_auth_token", "message": "expired"})

    storage = B2ObjectStorage(CONFIG, client=_client(handler))
    try:
        storage.list_object_versions("cells/aaaaaaaaaaaaaaaa/")
        raised = False
    except httpx.HTTPStatusError:
        raised = True
    assert raised is True
    assert calls["authorize"] == 2  # initial + the one retry, never more
