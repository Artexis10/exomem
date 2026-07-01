"""SingleUserGitHubVerifier's TTL cache for validated GitHub tokens.

Most tests stub the real GitHub round-trip (`super().verify_token`) with a call-counter
so they assert the *caching contract* without any network: a validated token is served
from cache within the TTL, distinct tokens validate independently, rejections are never
cached (instant recovery), and TTL=0 disables caching. The final test deliberately does
*not* stub `super().verify_token` (only the HTTP call) so the real parent code path runs
— a regression guard for the `_cache` attribute-shadowing bug those stubbed tests missed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastmcp.server.auth.providers.github import GitHubTokenVerifier
from fastmcp.utilities.token_cache import TokenCache

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
    (key,) = v._login_cache.keys()
    ts, access = v._login_cache[key]
    v._login_cache[key] = (ts - 10_000.0, access)
    asyncio.run(v.verify_token("tok"))

    assert calls["n"] == 2  # expired → re-hit GitHub


def test_github_rejection_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_super(monkeypatch, login=None)  # GitHub says no
    v = _verifier()

    assert asyncio.run(v.verify_token("tok")) is None
    assert asyncio.run(v.verify_token("tok")) is None
    assert calls["n"] == 2  # re-checked each time — recovery is instant after re-auth
    assert not v._login_cache


def test_wrong_login_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_super(monkeypatch, login="someone-else")
    v = _verifier()

    assert asyncio.run(v.verify_token("tok")) is None
    assert asyncio.run(v.verify_token("tok")) is None
    assert calls["n"] == 2  # only the *allowed* login is ever cached
    assert not v._login_cache


def test_ttl_zero_disables_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_MCP_AUTH_CACHE_TTL", "0")
    calls = _stub_super(monkeypatch, login=ALLOWED)
    v = _verifier()

    asyncio.run(v.verify_token("tok"))
    asyncio.run(v.verify_token("tok"))

    assert calls["n"] == 2  # caching off → every call revalidates
    assert not v._login_cache


def test_real_super_verify_does_not_crash_on_cache_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: do NOT shadow the parent GitHubTokenVerifier's ``_cache``.

    Every other test in this file replaces ``GitHubTokenVerifier.verify_token``
    wholesale, so none of them exercise the parent's first line —
    ``is_cached, cached_result = self._cache.get(token)``. When exomem's verifier
    overwrote ``self._cache`` with a plain ``dict``, that line unpacked
    ``dict.get(token)``'s ``None`` return → ``TypeError: cannot unpack
    non-iterable NoneType object`` → the OAuth token-swap failed → every
    authenticated ``/mcp`` request returned 401. This test runs the REAL
    ``super().verify_token`` (only the GitHub HTTP call is stubbed) so the
    regression is caught locally.
    """

    class _FakeResp:
        status_code = 401
        text = "unauthorized"
        headers: dict[str, str] = {}

        def json(self) -> dict[str, object]:  # pragma: no cover — 401 path returns early
            return {}

    async def _fake_get(self, url, **kwargs):  # noqa: ANN001 — test stub
        return _FakeResp()

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)

    v = _verifier()
    # A GitHub-rejected token must resolve to None, not raise the unpack TypeError
    # the parent's `self._cache.get(token)` throws when `_cache` is a plain dict.
    assert asyncio.run(v.verify_token("tok")) is None
    # And the parent class must still own ``_cache`` as its TokenCache.
    assert isinstance(v._cache, TokenCache)
