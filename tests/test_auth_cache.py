"""One-shot GitHub identity verification must never cache bearer material."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp.server.auth.providers.github import GitHubTokenVerifier

from exomem.server_auth import SingleUserGitHubVerifier

ALLOWED_LOGIN = "person"
ALLOWED_ID = 123456


def _verifier() -> SingleUserGitHubVerifier:
    return SingleUserGitHubVerifier(
        allowed_login=ALLOWED_LOGIN,
        allowed_user_id=ALLOWED_ID,
    )


def _stub_super(
    monkeypatch: pytest.MonkeyPatch,
    *,
    login: str | None = ALLOWED_LOGIN,
    user_id: object = ALLOWED_ID,
) -> dict[str, int]:
    calls = {"count": 0}

    async def fake(self, token):  # noqa: ANN001
        del self
        calls["count"] += 1
        if login is None:
            return None
        return SimpleNamespace(
            claims={"login": login, "sub": str(user_id)},
            client_id=str(user_id),
            token=token,
        )

    monkeypatch.setattr(GitHubTokenVerifier, "verify_token", fake)
    return calls


def test_exact_login_and_immutable_id_are_required(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_super(monkeypatch)
    verifier = _verifier()

    result = asyncio.run(verifier.verify_token("temporary-github-token"))

    assert result is not None
    assert calls["count"] == 1


@pytest.mark.parametrize(
    ("login", "user_id"),
    [
        ("someone-else", ALLOWED_ID),
        (None, ALLOWED_ID),
        (ALLOWED_LOGIN, 999),
        (ALLOWED_LOGIN, None),
        (ALLOWED_LOGIN, "not-numeric"),
    ],
)
def test_wrong_or_missing_identity_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    login: str | None,
    user_id: object,
) -> None:
    _stub_super(monkeypatch, login=login, user_id=user_id)

    assert asyncio.run(_verifier().verify_token("temporary-github-token")) is None


def test_verification_cache_is_disabled_and_no_exomem_cache_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_super(monkeypatch)
    verifier = _verifier()

    first = asyncio.run(verifier.verify_token("same-token"))
    second = asyncio.run(verifier.verify_token("same-token"))

    assert first is not None and second is not None
    assert calls["count"] == 2
    assert verifier._cache.enabled is False
    assert verifier._cache._entries == {}
    assert not hasattr(verifier, "_login_cache")


def test_boolean_claim_is_not_accepted_as_numeric_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake(self, token):  # noqa: ANN001
        del self
        return SimpleNamespace(
            claims={"login": ALLOWED_LOGIN, "sub": True},
            client_id="1",
            token=token,
        )

    monkeypatch.setattr(GitHubTokenVerifier, "verify_token", fake)
    verifier = SingleUserGitHubVerifier(allowed_login=ALLOWED_LOGIN, allowed_user_id=1)

    assert asyncio.run(verifier.verify_token("temporary-github-token")) is None


@pytest.mark.parametrize("user_id", [0, -1, True])
def test_verifier_refuses_non_positive_immutable_id(user_id: object) -> None:
    with pytest.raises(ValueError, match="positive numeric"):
        SingleUserGitHubVerifier(allowed_login=ALLOWED_LOGIN, allowed_user_id=user_id)
