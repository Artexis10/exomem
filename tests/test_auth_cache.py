"""One-shot GitHub identity verification must never cache bearer material."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from exomem.server_auth import SingleUserGitHubVerifier

ALLOWED_LOGIN = "person"
ALLOWED_ID = 123456


def _response(
    request: httpx.Request,
    *,
    login: str | None = ALLOWED_LOGIN,
    user_id: Any = ALLOWED_ID,
    status_code: int = 200,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"login": login, "id": user_id},
        request=request,
    )


@pytest.mark.anyio
async def test_exact_identity_uses_exactly_one_uncached_github_user_request() -> None:
    requests: list[httpx.Request] = []

    def github(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        verifier = SingleUserGitHubVerifier(
            allowed_login=ALLOWED_LOGIN,
            allowed_user_id=ALLOWED_ID,
            http_client=client,
        )
        result = await verifier.verify_token("temporary-github-token")

    assert result is not None
    assert result.client_id == str(ALLOWED_ID)
    assert result.scopes == []
    assert result.claims == {"sub": str(ALLOWED_ID), "login": ALLOWED_LOGIN}
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url == "https://api.github.com/user"
    assert requests[0].headers["authorization"] == "Bearer temporary-github-token"
    assert verifier._cache.enabled is False
    assert verifier._cache._entries == {}
    assert not hasattr(verifier, "_login_cache")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("login", "user_id"),
    [
        ("someone-else", ALLOWED_ID),
        (None, ALLOWED_ID),
        (ALLOWED_LOGIN, 999),
        (ALLOWED_LOGIN, None),
        (ALLOWED_LOGIN, "not-numeric"),
        (ALLOWED_LOGIN, True),
    ],
)
async def test_wrong_missing_or_non_numeric_identity_is_rejected(
    login: str | None,
    user_id: Any,
) -> None:
    requests = 0

    def github(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return _response(request, login=login, user_id=user_id)

    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        verifier = SingleUserGitHubVerifier(
            allowed_login=ALLOWED_LOGIN,
            allowed_user_id=ALLOWED_ID,
            http_client=client,
        )
        assert await verifier.verify_token("temporary-github-token") is None

    assert requests == 1


@pytest.mark.anyio
async def test_same_proof_is_fetched_once_per_verification_without_scope_request() -> None:
    paths: list[str] = []

    def github(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return _response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        verifier = SingleUserGitHubVerifier(
            allowed_login=ALLOWED_LOGIN,
            allowed_user_id=ALLOWED_ID,
            http_client=client,
        )
        first = await verifier.verify_token("same-token")
        second = await verifier.verify_token("same-token")

    assert first is not None and second is not None
    assert paths == ["/user", "/user"]
    assert verifier._cache.enabled is False
    assert verifier._cache._entries == {}


@pytest.mark.anyio
@pytest.mark.parametrize("status_code", [401, 403, 429, 503])
async def test_github_failure_alert_is_secret_safe(
    status_code: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bearer = "temporary-github-token-must-not-be-logged"

    def github(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            text=f"provider echoed {bearer}",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(github)) as client:
        verifier = SingleUserGitHubVerifier(
            allowed_login=ALLOWED_LOGIN,
            allowed_user_id=ALLOWED_ID,
            http_client=client,
        )
        assert await verifier.verify_token(bearer) is None

    assert bearer not in caplog.text
    assert str(status_code) in caplog.text


@pytest.mark.parametrize("user_id", [0, -1, True])
def test_verifier_refuses_non_positive_immutable_id(user_id: object) -> None:
    with pytest.raises(ValueError, match="positive numeric"):
        SingleUserGitHubVerifier(allowed_login=ALLOWED_LOGIN, allowed_user_id=user_id)
