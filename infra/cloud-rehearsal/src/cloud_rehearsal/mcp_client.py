"""A real OAuth MCP client against the local stack.

The MCP Python SDK's `OAuthClientProvider` does everything a connector does:
the gateway's 401, protected-resource discovery, Substrate's
authorization-server metadata, PKCE (S256), the authorization request with
`resource`, the code exchange, and bearer use. Only the human's part is
scripted, by `HeadlessBrowser`, which does what the consent page's own form
does: it follows the authorization redirect with the tenant's session
cookie, reads the hidden `nonce` and `confirmation` fields Substrate
rendered, and submits them.

The OAuth client is pinned (Substrate has no dynamic registration). Its
information is handed to the SDK through `TokenStorage.get_client_info`,
which the SDK treats as an existing registration.
"""

from __future__ import annotations

import asyncio
import contextlib
import html.parser
import json
import time
import urllib.parse
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from .http import Resolver
from .substrate import MCP_URL, OAUTH_CLIENT_ID, PUBLIC_BASE_URL

SCOPES = "exomem.read exomem.write"


class _HiddenInputs(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.form_action: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form" and values.get("action", "").endswith("/authorize/complete"):
            self.form_action = values["action"]
        if tag == "input" and values.get("type") == "hidden" and values.get("name"):
            self.fields[values["name"] or ""] = values.get("value") or ""


@dataclass
class BrowserSession:
    """A signed-in tenant's browser cookies on the Substrate origin."""

    cookies: dict[str, str]

    @property
    def csrf(self) -> str:
        return self.cookies["exomem_csrf"]


class HeadlessBrowser:
    def __init__(self, resolver: Resolver) -> None:
        self._resolver = resolver

    def _client(self, cookies: dict[str, str]) -> httpx.AsyncClient:
        return self._resolver.async_client(cookies=cookies, follow_redirects=False)

    async def redeem_invite(self, token: str) -> BrowserSession:
        """The invite link's page: POST /api/exomem/access/redeem."""

        async with self._client({}) as client:
            response = await client.post(
                f"{PUBLIC_BASE_URL}/api/exomem/access/redeem",
                json={"token": token},
                headers={"origin": PUBLIC_BASE_URL},
            )
            if response.status_code != 200:
                raise RuntimeError(f"invite redemption answered {response.status_code}: {_error_code(response)}")
            return BrowserSession(cookies=dict(client.cookies.items()))

    async def consent(self, session: BrowserSession, authorization_url: str, redirect_uri: str) -> tuple[str, str | None]:
        """Follows the authorization request to the consent page and submits it.

        Substrate must send the code to exactly the registered redirect URI
        (scheme, host, port and path), as a loopback client would receive it.
        """

        async with self._client(session.cookies) as client:
            first = await client.get(authorization_url)
            if first.status_code not in (302, 303):
                raise RuntimeError(f"authorize answered {first.status_code}: {_error_code(first)}")
            page_url = urllib.parse.urljoin(authorization_url, first.headers["location"])
            page = await client.get(page_url)
            if page.status_code != 200:
                raise RuntimeError(f"consent page answered {page.status_code}")
            parser = _HiddenInputs()
            parser.feed(page.text)
            if parser.form_action is None or "nonce" not in parser.fields:
                raise RuntimeError("the consent page rendered no continue form (is the session signed in?)")
            complete = await client.post(
                urllib.parse.urljoin(PUBLIC_BASE_URL, parser.form_action),
                data={"nonce": parser.fields["nonce"], "confirmation": parser.fields.get("confirmation", "")},
                headers={"origin": PUBLIC_BASE_URL, "sec-fetch-site": "same-origin"},
            )
            if complete.status_code not in (302, 303):
                raise RuntimeError(f"consent completion answered {complete.status_code}: {_error_code(complete)}")
            redirect = urllib.parse.urlparse(complete.headers["location"])
            landed = f"{redirect.scheme}://{redirect.netloc}{redirect.path}"
            if landed != redirect_uri:
                raise RuntimeError(f"consent redirected to {landed}, not the registered {redirect_uri}")
            query = urllib.parse.parse_qs(redirect.query)
            if "code" not in query:
                raise RuntimeError(f"consent redirected without a code: {query.get('error')}")
            return query["code"][0], query.get("state", [None])[0]

    async def post_with_session(self, session: BrowserSession, path: str, payload: dict[str, Any]) -> httpx.Response:
        async with self._client(session.cookies) as client:
            return await client.post(
                f"{PUBLIC_BASE_URL}{path}",
                json=payload,
                headers={"origin": PUBLIC_BASE_URL, "x-exomem-csrf": session.csrf},
            )


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return "<non-JSON body>"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("code"))
    return str(error or body.get("code") if isinstance(body, dict) else body)[:200]


@dataclass
class _Storage:
    client_info: OAuthClientInformationFull
    tokens: OAuthToken | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


@dataclass
class ToolResult:
    structured: dict[str, Any]
    is_error: bool
    seconds: float

    def text(self) -> str:
        return json.dumps(self.structured, sort_keys=True)

    @property
    def error_code(self) -> str | None:
        error = self.structured.get("error")
        return error.get("code") if isinstance(error, dict) else None


@dataclass
class TenantClient:
    """One tenant's connector: OAuth once, then MCP sessions on its token."""

    resolver: Resolver
    browser: HeadlessBrowser
    session: BrowserSession
    redirect_uri: str
    storage: _Storage = field(init=False)
    authorizations: int = 0
    # What the stock SDK sent at the token endpoint, never the verifier itself.
    verifier_seen: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.storage = _Storage(
            client_info=OAuthClientInformationFull(
                client_id=OAUTH_CLIENT_ID,
                redirect_uris=[self.redirect_uri],
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                scope=SCOPES,
            )
        )
        self._pending: asyncio.Future[tuple[str, str | None]] | None = None
        self._http: httpx.AsyncClient | None = None

    async def _redirect(self, authorization_url: str) -> None:
        self.authorizations += 1
        loop = asyncio.get_running_loop()
        self._pending = loop.create_future()
        try:
            self._pending.set_result(await self.browser.consent(self.session, authorization_url, self.redirect_uri))
        except Exception as error:  # noqa: BLE001 - surfaced through the callback
            self._pending.set_exception(error)

    async def _callback(self) -> tuple[str, str | None]:
        assert self._pending is not None
        return await self._pending

    def _auth(self) -> OAuthClientProvider:
        return OAuthClientProvider(
            server_url=MCP_URL,
            client_metadata=OAuthClientMetadata(
                client_name="Exomem Cloud rehearsal",
                redirect_uris=[self.redirect_uri],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
                scope=SCOPES,
            ),
            storage=self.storage,
            redirect_handler=self._redirect,
            callback_handler=self._callback,
        )

    @property
    def access_token(self) -> str | None:
        return self.storage.tokens.access_token if self.storage.tokens else None

    def _http_client(self) -> httpx.AsyncClient:
        """One client per connector, as a real connector keeps its connections."""

        if self._http is None or self._http.is_closed:
            self._http = self.resolver.async_client(
                auth=self._auth(), timeout=httpx.Timeout(120.0), event_hooks={"request": [self._observe_token_request]}
            )
        return self._http

    async def _observe_token_request(self, request: httpx.Request) -> None:
        """Records the PKCE verifier's shape from the SDK's own token request."""

        if request.method != "POST" or not request.url.path.endswith("/token"):
            return
        body = await request.aread()
        verifier = urllib.parse.parse_qs(body.decode("utf-8", "replace")).get("code_verifier", [""])[0]
        if verifier:
            self.verifier_seen = {"length": len(verifier), "uses_dot_or_tilde": any(c in verifier for c in ".~")}

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @contextlib.asynccontextmanager
    async def mcp(self) -> AsyncIterator[MeasuredSession]:
        async with streamable_http_client(MCP_URL, http_client=self._http_client()) as (read, write, _):
            async with ClientSession(read, write) as session:
                started = time.perf_counter()
                await session.initialize()
                yield MeasuredSession(session, initialize_seconds=time.perf_counter() - started)


@dataclass
class MeasuredSession:
    session: ClientSession
    initialize_seconds: float

    async def list_tools(self) -> tuple[list[str], float]:
        started = time.perf_counter()
        result = await self.session.list_tools()
        return sorted(tool.name for tool in result.tools), time.perf_counter() - started

    async def call(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        started = time.perf_counter()
        result = await self.session.call_tool(name, arguments)
        elapsed = time.perf_counter() - started
        structured = result.structuredContent or {}
        if not structured and result.content:
            text = getattr(result.content[0], "text", "")
            try:
                structured = json.loads(text)
            except (TypeError, ValueError):
                structured = {"text": text}
        return ToolResult(structured=structured, is_error=bool(result.isError), seconds=elapsed)

    async def governed_write(self, arguments: dict[str, Any], *, timeout: float = 90.0) -> ToolResult:
        """`remember`, honouring MUTATION_WARMING's own retry_after_ms."""

        deadline = time.monotonic() + timeout
        while True:
            result = await self.call("remember", arguments)
            if result.error_code != "MUTATION_WARMING" or time.monotonic() > deadline:
                return result
            wait_ms = (result.structured.get("error") or {}).get("retry_after_ms", 500)
            await asyncio.sleep(max(0.1, wait_ms / 1000))


async def raw_mcp_post(resolver: Resolver, *, token: str | None, headers: dict[str, str] | None = None) -> httpx.Response:
    """One raw initialize POST to the gateway, for denial checks."""

    request_headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    if token:
        request_headers["authorization"] = f"Bearer {token}"
    request_headers.update(headers or {})
    async with resolver.async_client() as client:
        return await client.post(
            MCP_URL,
            headers=request_headers,
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "rehearsal-denial-probe", "version": "1"}},
            },
        )
