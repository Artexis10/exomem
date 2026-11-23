"""A cloud cell relays the gateway's service notice in its MCP results.

The gateway forwards the current maintenance or incident notice in `x-exomem-service-notice`
as base64url-encoded UTF-8 JSON. These cases send requests the way the gateway forwards them
and read what the connected assistant would read back.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from _public_call import public_call
from test_cloud_cell import LEGACY_VERSION, TOKEN, _build_cloud_server, _teardown_activation

from exomem import call_ledger, cloud_cell, server

MAINTENANCE = {
    "v": 1,
    "kind": "maintenance",
    "state": "scheduled",
    "starts_at": "2026-11-25T02:00:00Z",
    "ends_at": "2026-11-25T02:30:00Z",
    "say": "Exomem Cloud will be briefly unavailable on 25 November from 02:00 to 02:30 UTC.",
}
INCIDENT = {**MAINTENANCE, "kind": "incident", "state": "active", "ends_at": None,
            "say": "Exomem Cloud is slow right now. Your memory is safe."}


def relayed(notice: dict) -> dict:
    """What a result carries: the notice without its protocol version."""
    return {key: value for key, value in notice.items() if key != "v"}


def encode(notice: object, *, padded: bool = False) -> str:
    """The gateway's header value: base64url of UTF-8 JSON."""
    token = base64.urlsafe_b64encode(
        json.dumps(notice, separators=(",", ":"), ensure_ascii=False).encode()
    ).decode()
    return token if padded else token.rstrip("=")


@pytest.fixture
def run_cell(vault, monkeypatch, tmp_path):
    """Run a scenario against a cell (or, with `cloud=False`, a personal service) over HTTP."""
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "machine.json"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    call_ledger.reset_chain_cache()
    started = []

    def run(scenario, *, cloud: bool = True, read_only: bool = False):
        if cloud:
            mcp, _ = _build_cloud_server(monkeypatch, read_only=read_only)
        else:
            monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
            monkeypatch.delenv("EXOMEM_CLOUD_CELL", raising=False)
            mcp = server.build_server(require_auth=False)
        started.append(mcp)
        app = mcp.http_app(stateless_http=True, json_response=True)

        async def wrapped():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
                ) as client:
                    return await scenario(client)

        return asyncio.run(wrapped())

    yield run
    for mcp in started:
        _teardown_activation(mcp)


async def call(client, name, arguments, *, notice: str | None = None) -> dict:
    """One tools/call as the gateway forwards it; the MCP result, whose call itself succeeded."""
    name, arguments = public_call(name, arguments)
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "authorization": f"Bearer {TOKEN}",
        "mcp-protocol-version": LEGACY_VERSION,
    }
    if notice is not None:
        headers[cloud_cell.GATEWAY_SERVICE_NOTICE_HEADER] = notice
    response = await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False, result
    return result


SEARCH = ("search_memory", {"query": "anything"})
SAVE = ("create_note", {
    "content": "## Observations\n\n- [operating constraint] Keep retries bounded #reliability\n",
    "title": "Notice Test",
    "unlinked_reason": "synthetic",
})
TRANSFER = ("transfer_memory", {"action": "export_request", "confirm": True})


def test_a_cloud_cell_relays_a_valid_notice_in_every_object_result(run_cell):
    """The assistant tells the person only what a result carries, so a read, a save and a
    refusal must each carry the notice, and a request without one must carry none."""

    async def scenario(client):
        header = encode(MAINTENANCE)
        return {
            "recall": await call(client, "recall_context", {"turn": "anything"}, notice=header),
            "search": await call(client, *SEARCH, notice=header),
            "save": await call(client, *SAVE, notice=header),
            "refusal": await call(client, *TRANSFER, notice=header),
            "padded": await call(client, *SEARCH, notice=encode(MAINTENANCE, padded=True)),
            "none": await call(client, *SEARCH),
        }

    results = run_cell(scenario)

    assert results["save"]["structuredContent"]["user_summary"].startswith("Saved")
    assert results["refusal"]["structuredContent"]["error"]["code"] == "CLOUD_GATEWAY_REQUIRED"
    for name in ("recall", "save", "refusal"):
        assert results[name]["structuredContent"]["service_notice"] == relayed(MAINTENANCE), name
    # `search_memory` publishes its object under `result`; the notice rides inside it, with the hits.
    for name in ("search", "padded"):
        assert results[name]["structuredContent"]["result"]["service_notice"] == relayed(MAINTENANCE), name
    # A host that reads only the text content must see the notice too.
    text = json.loads(results["save"]["content"][0]["text"])
    assert text["service_notice"] == relayed(MAINTENANCE)
    assert "service_notice" not in json.dumps(results["none"])


def test_a_refused_save_carries_an_incident_notice(run_cell):
    async def scenario(client):
        return await call(client, *SAVE, notice=encode(INCIDENT))

    result = run_cell(scenario, read_only=True)["structuredContent"]

    assert result["error"]["code"] == "CLOUD_CELL_READ_ONLY"
    assert result["service_notice"] == relayed(INCIDENT)
    assert result["service_notice"]["ends_at"] is None


def test_a_call_refused_before_its_command_runs_carries_the_notice(run_cell):
    """A door call that no operation owns is refused in middleware, outside the command wrapper."""

    async def scenario(client):
        return await call(
            client, "inspect_memory", {"operation": "no.such.operation", "arguments": {}},
            notice=encode(MAINTENANCE),
        )

    result = run_cell(scenario)["structuredContent"]

    assert "error" in result, result
    assert result["service_notice"] == relayed(MAINTENANCE)


@pytest.mark.parametrize("header", [
    "not base64 at all!",
    encode("not an object"),
    encode({**MAINTENANCE, "v": 2}),
    "A" * (cloud_cell.SERVICE_NOTICE_MAX_HEADER_BYTES + 1),
])
def test_a_cloud_cell_serves_the_call_when_the_notice_is_unusable(run_cell, header):
    async def scenario(client):
        return await call(client, *SEARCH, notice=header)

    result = run_cell(scenario)

    assert "service_notice" not in json.dumps(result)


def test_a_personal_service_ignores_the_notice_header(run_cell):
    """Nothing vouches for the header outside a cell, where any caller can send it."""

    async def scenario(client):
        return await call(client, *SEARCH, notice=encode(MAINTENANCE))

    result = run_cell(scenario, cloud=False)

    assert "service_notice" not in json.dumps(result)


@pytest.mark.parametrize("value, accepted", [
    (encode(MAINTENANCE), True),
    (encode(MAINTENANCE, padded=True), True),
    (encode(INCIDENT), True),
    (encode({**MAINTENANCE, "say": "x" * 400}), True),
    (encode({**MAINTENANCE, "say": "x" * 401}), False),
    (encode({**MAINTENANCE, "say": ""}), False),
    (encode({**MAINTENANCE, "v": 2}), False),
    (encode({**MAINTENANCE, "v": True}), False),
    (encode({**MAINTENANCE, "v": 1.0}), False),
    (encode({**MAINTENANCE, "kind": "outage"}), False),
    (encode({**MAINTENANCE, "kind": ["maintenance"]}), False),
    (encode({**MAINTENANCE, "state": "over"}), False),
    (encode({**MAINTENANCE, "starts_at": "tomorrow"}), False),
    (encode({**MAINTENANCE, "starts_at": None}), False),
    (encode({**MAINTENANCE, "ends_at": "later"}), False),
    (encode({**MAINTENANCE, "extra": "field"}), False),
    (encode({key: value for key, value in MAINTENANCE.items() if key != "ends_at"}), False),
    (encode([MAINTENANCE]), False),
    # A valid notice whose header is over the limit: 400 three-byte characters.
    (encode({**MAINTENANCE, "say": "字" * 400}), False),
    ("+" + encode(MAINTENANCE), False),
    (base64.urlsafe_b64encode(b"\xff\xfe not utf-8").decode(), False),
    (base64.urlsafe_b64encode(json.dumps(MAINTENANCE).encode("utf-16")).decode(), False),
    # JSON can spell a lone surrogate, which no UTF-8 reply can carry.
    (base64.urlsafe_b64encode(json.dumps({**MAINTENANCE, "say": "\ud800"}).encode()).decode(), False),
    ("", False),
    (None, False),
])
def test_only_a_notice_of_the_published_shape_is_relayed(value, accepted):
    notice = cloud_cell.parse_service_notice(value)

    assert (notice is not None) is accepted
    if accepted:
        assert set(notice) == {"kind", "state", "starts_at", "ends_at", "say"}
