"""Exercise owner routes through the actual FastMCP composition root."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from test_native_owner_integration import managed  # noqa: F401
from test_session_oauth import _proxy

from exomem import schema, server
from exomem.governance import policy
from exomem.server_auth import SingleUserGitHubVerifier
from exomem.server_runtime import ServerRuntime


@pytest.fixture
def composed_owner(managed, monkeypatch):  # noqa: F811 - imported pytest fixture
    control, _unit = managed
    proxy = _proxy(verifier=SingleUserGitHubVerifier(allowed_login="person", allowed_user_id=123))
    runtime = ServerRuntime(
        vault_root=control.vault_root,
        source_schema=schema.load_source_schema(control.vault_root),
        project_keys_hint="",
        base_url="https://memory.example",
    )
    monkeypatch.setattr(server, "initialize_runtime", lambda **_kwargs: runtime)
    monkeypatch.setattr(server, "build_oauth", lambda **_kwargs: proxy)
    return control, proxy


@pytest.mark.anyio
async def test_composed_server_reviews_and_accepts_policy_through_owner_browser(composed_owner):
    control, proxy = composed_owner
    app = server.build_server(require_auth=True)

    async def verified_identity(_code, _transaction):
        # Only the external identity exchange is substituted. Owner transaction,
        # cookies, CSRF, review store and canonical policy commit are real.
        return {"exomem_identity": {"github_user_id": 123, "github_login": "person"}}

    proxy._exchange_identity = verified_identity
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.http_app()), base_url="https://memory.example"
    ) as browser:
        unsigned = await browser.get("/owner")
        assert unsigned.status_code == 303
        assert unsigned.headers["location"] == "/owner/login"
        login = await browser.get("/owner/login")
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        callback = await browser.get(
            "/auth/callback", params={"state": state, "code": "fixture-identity-code"}
        )
        assert callback.status_code == 302
        assert callback.headers["location"] == "/owner"
        home = await browser.get("/owner")
        assert home.status_code == 200
        assert "Review initial policy" in home.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', home.text).group(1)
        assert policy.load(control.vault_root).empty
        prepared = await browser.post(
            "/owner/review",
            data={"csrf": csrf, "action": "policy"},
            headers={"Origin": "https://memory.example"},
        )
        assert prepared.status_code == 303
        review_path = prepared.headers["location"]
        review = await browser.get(review_path)
        assert review.status_code == 200
        assert "Existing access" in review.text
        assert policy.load(control.vault_root).empty
        rejected = await browser.post(
            review_path + "/accept",
            data={"csrf": csrf},
            headers={"Origin": "https://other.example"},
        )
        assert rejected.status_code == 403
        assert policy.load(control.vault_root).empty
        accepted = await browser.post(
            review_path + "/accept",
            data={"csrf": csrf},
            headers={"Origin": "https://memory.example"},
        )
        assert accepted.status_code == 303
        assert not policy.load(control.vault_root).empty
        assert (await browser.get("/owner")).status_code == 200

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.http_app()), base_url="https://memory.example"
    ) as agent:
        denied = await agent.post(
            review_path + "/accept",
            data={"csrf": csrf},
            headers={"Origin": "https://memory.example", "Authorization": "Bearer agent-token"},
        )
        assert denied.status_code == 401
    assert not proxy._session_authority.issue_calls


@pytest.mark.anyio
async def test_unconfigured_owner_surface_is_absent(composed_owner, monkeypatch):
    _control, proxy = composed_owner
    monkeypatch.delenv("EXOMEM_VOCABULARY_AUTHORITY_DIR")
    app = server.build_server(require_auth=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.http_app()), base_url="https://memory.example"
    ) as browser:
        assert (await browser.get("/owner")).status_code == 404
        assert (await browser.get("/owner/login")).status_code == 404
    assert proxy._owner_auth is None


@pytest.mark.anyio
async def test_unauthenticated_local_server_never_exposes_owner_controls(
    composed_owner, monkeypatch
):
    monkeypatch.setattr(server, "build_oauth", lambda **_kwargs: None)
    app = server.build_server(require_auth=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.http_app()), base_url="https://memory.example"
    ) as browser:
        assert (await browser.get("/owner")).status_code == 404
        assert (
            await browser.post("/owner/review", data={"action": "activation"})
        ).status_code == 404
