"""A modern tools/call is checked against the advertised schema without listing the catalog.

The MCP SDK validates `Mcp-Param-*` headers against the called tool's input schema. Until the
server answers that lookup itself, the SDK runs a complete `tools/list` through the server
before every call that carries arguments. These tests drive the stateless HTTP entry the
shipped service uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
import pytest
from fastmcp.server.middleware.middleware import Middleware
from mcp_types import HEADER_MISMATCH
from pydantic import Field

from exomem import call_ledger, writer_lease
from exomem import server as server_module

MODERN_VERSION = "2026-07-28"
ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "schema-lookup-test", "version": "1"},
}

# A tool that declares a header-mirrored parameter: the only way the SDK can reject a header.
Region = Annotated[str, Field(json_schema_extra={"x-mcp-header": "Region"})]


class _ListingCounter(Middleware):
    """Counts every tools/list that reaches FastMCP, whoever asked for it."""

    def __init__(self) -> None:
        self.count = 0

    async def on_list_tools(self, context, call_next):
        self.count += 1
        return await call_next(context)


def _probe_server() -> tuple[server_module.ExomemFastMCP, _ListingCounter]:
    mcp = server_module.ExomemFastMCP("schema-lookup-probe")
    listings = _ListingCounter()
    mcp.add_middleware(listings)

    @mcp.tool(name="routed")
    async def routed(region: Region, note: str = "") -> dict[str, str]:
        return {"region": region, "note": note}

    return mcp, listings


async def _unavailable() -> None:
    raise RuntimeError("the catalog could not be read")


@asynccontextmanager
async def _serving(mcp: server_module.ExomemFastMCP) -> AsyncIterator[httpx.AsyncClient]:
    app = mcp.http_app(stateless_http=True, json_response=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://probe.example"
        ) as client:
            yield client


async def _post(
    client: httpx.AsyncClient,
    method: str,
    params: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        "/mcp",
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "mcp-protocol-version": MODERN_VERSION,
            "mcp-method": method,
            **({"mcp-name": params["name"]} if method == "tools/call" else {}),
            **(headers or {}),
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {"_meta": ENVELOPE, **params},
        },
    )


async def _call_routed(client: httpx.AsyncClient, *, argument: str, header: str) -> httpx.Response:
    return await _post(
        client,
        "tools/call",
        {"name": "routed", "arguments": {"region": argument}},
        {"mcp-param-region": header},
    )


@pytest.mark.anyio
async def test_a_call_with_arguments_does_not_list_the_catalog() -> None:
    mcp, listings = _probe_server()

    async with _serving(mcp) as client:
        listed_at_startup = listings.count
        response = await _call_routed(client, argument="eu", header="eu")

    assert response.status_code == 200, response.text
    assert response.json()["result"]["structuredContent"] == {"region": "eu", "note": ""}
    assert listings.count == listed_at_startup


@pytest.mark.anyio
async def test_until_the_lookup_exists_the_sdk_lists_the_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp, listings = _probe_server()
    monkeypatch.setattr(mcp, "publish_tool_input_schemas", _unavailable)

    async with _serving(mcp) as client:
        assert mcp._mcp_server.get_tool_input_schema is None
        listed_at_startup = listings.count
        response = await _call_routed(client, argument="eu", header="eu")

    assert response.status_code == 200, response.text
    assert listings.count == listed_at_startup + 1


@pytest.mark.anyio
@pytest.mark.parametrize("lookup", ["published", "sdk listing"])
async def test_header_validation_accepts_and_rejects_the_same_calls_on_either_path(
    lookup: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mcp, _ = _probe_server()
    if lookup == "sdk listing":
        monkeypatch.setattr(mcp, "publish_tool_input_schemas", _unavailable)

    async with _serving(mcp) as client:
        assert (mcp._mcp_server.get_tool_input_schema is not None) is (lookup == "published")
        agreeing = await _call_routed(client, argument="eu", header="eu")
        disagreeing = await _call_routed(client, argument="eu", header="us")

    assert agreeing.status_code == 200, agreeing.text
    assert disagreeing.status_code == 400, disagreeing.text
    assert disagreeing.json()["error"]["code"] == HEADER_MISMATCH
    assert "Mcp-Param-Region" in disagreeing.json()["error"]["message"]


@pytest.fixture
def shipped_server(vault, monkeypatch, tmp_path):
    """The shipped server over the test vault, built as the service builds it."""
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    monkeypatch.delenv("EXOMEM_MCP_TOOL_SHAPE", raising=False)
    monkeypatch.delenv("EXOMEM_MCP_LEGACY_COMPAT", raising=False)
    call_ledger.reset_chain_cache()
    yield server_module.build_server(require_auth=False)
    writer_lease.reset_managers_for_tests()


@pytest.mark.anyio
async def test_the_lookup_returns_the_schema_each_advertised_tool_lists(shipped_server) -> None:
    async with _serving(shipped_server) as client:
        listing = await _post(client, "tools/list", {})
        lookup = shipped_server._mcp_server.get_tool_input_schema

    advertised = {tool["name"]: tool["inputSchema"] for tool in listing.json()["result"]["tools"]}
    assert advertised
    assert lookup is not None
    assert {name: lookup(name) for name in advertised} == advertised
    # A name the listing hides stays callable and unvalidated, as it was under the SDK listing.
    assert "record_memory" not in advertised
    assert lookup("record_memory") is None
