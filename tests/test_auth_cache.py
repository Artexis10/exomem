"""SingleUserGitHubVerifier's TTL cache for validated GitHub tokens.

The real GitHub round-trip (`super().verify_token`) is stubbed with a call-counter so
these tests assert the *caching contract* without any network: a validated token is
served from cache within the TTL, distinct tokens validate independently, rejections are
never cached (instant recovery), and TTL=0 disables caching.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp.server.auth.providers.github import GitHubTokenVerifier

from kb_mcp.server import SingleUserGitHubVerifier

ALLOWED = "artexis10"


def _verifier() -> SingleUserGitHubVerifier:
    return SingleUserGitHubVerifier(allowed_login=ALLOWED)


def _stub_super(monkeypatch: pytest.MonkeyPatch, *, login: str | None):
    """Patch the parent verify_token to count calls; returns the call counter.

    `login=None` simulates GitHub rejecting the token (returns None)."""
    calls = {"n": 0}

    async def fake(self, token):  # noqa: ANN001 — test stub
        calls["n"] += 1
        if login is None:
            return None
        return SimpleNamespace(claims={"login": login}, token=token)

    monkeypatch.setattr(GitHubTokenVerifier, "verify_token", fake)
    return calls


def test_validated_token_is_served_from_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_super(monkeypatch, login=ALLOWED)
    v = _verifier()

    a1 = asyncio.run(v.verify_token("tok"))
    a2 = asyncio.run(v.verify_token("tok"))

    assert a1 is not None and a2 is a1  # same cached object
    assert calls["n"] == 1  # second call hit the cache, not GitHub


def test_distinct_tokens_validate_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_super(monkeypatch, login=ALLOWED)
    v = _verifier()

    asyncio.run(v.verify_token("tok-a"))
    asyncio.run(v.verify_token("tok-b"))

    assert calls["n"] == 2  # different cache keys → each validated once


def test_expired_entry_revalidates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_super(monkeypatch, login=ALLOWED)
    v = _verifier()

    asyncio.run(v.verify_token("tok"))
    # Rewind the cached timestamp well past the TTL to simulate expiry.
    (key,) = v._cache.keys()
    ts, access = v._cache[key]
    v._cache[key] = (ts - 10_000.0, access)
    asyncio.run(v.verify_token("tok"))

    assert calls["n"] == 2  # expired → re-hit GitHub


def test_github_rejection_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_super(monkeypatch, login=None)  # GitHub says no
    v = _verifier()

    assert asyncio.run(v.verify_token("tok")) is None
    assert asyncio.run(v.verify_token("tok")) is None
    assert calls["n"] == 2  # re-checked each time — recovery is instant after re-auth
    assert not v._cache


def test_wrong_login_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_super(monkeypatch, login="someone-else")
    v = _verifier()

    assert asyncio.run(v.verify_token("tok")) is None
    assert asyncio.run(v.verify_token("tok")) is None
    assert calls["n"] == 2  # only the *allowed* login is ever cached
    assert not v._cache


def test_ttl_zero_disables_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_AUTH_CACHE_TTL", "0")
    calls = _stub_super(monkeypatch, login=ALLOWED)
    v = _verifier()

    asyncio.run(v.verify_token("tok"))
    asyncio.run(v.verify_token("tok"))

    assert calls["n"] == 2  # caching off → every call revalidates
    assert not v._cache
