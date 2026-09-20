from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from test_hosted_activation_proof import proof_case as proof_case

from exomem.hosted_activation_ack_protocol import PROTOCOL
from exomem.hosted_activation_proof import ActivationProofEndpoint, prove_publication


def test_registered_proof_route_uses_existing_private_authentication(tmp_path, monkeypatch):
    from test_hosted_private_routes import _cell, _headers

    monkeypatch.setenv("EXOMEM_HOSTED_ACTIVATION_ACK_PROTOCOL", PROTOCOL)
    monkeypatch.setenv("EXOMEM_HOSTED_ACTIVATION_ACK_SOCKET", "/run/exomem/activation-ack/ack.sock")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "cell-alpha" / "state"))
    client, config, _, _ = _cell(tmp_path, cell_id="cell-alpha", credential="private-proof-credential-sentinel")
    route = "/private/exomem/v1/activation/proof"
    refused = client.post(route, json={})
    assert refused.status_code == 401
    assert "private-proof-credential-sentinel" not in refused.text
    authenticated = client.post(route, headers=_headers(config), json={})
    assert authenticated.status_code == 400
    assert authenticated.json()["code"] == "MALFORMED_REQUEST"


@pytest.mark.anyio
async def test_reserved_route_returns_bare_proof(proof_case):
    request, arguments = proof_case
    endpoint = ActivationProofEndpoint(lambda body: prove_publication(body, **arguments))
    app = Starlette(routes=[Route("/proof", endpoint.handle, methods=["POST"])])
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.post("/proof", json=request)
        assert result.status_code == 200
        assert result.json()["challenge_id"] == request["challenge_id"]
        assert "data" not in result.json()
    finally:
        endpoint.close()


@pytest.mark.anyio
async def test_cancelled_proof_retains_slot_until_underlying_work_stops(proof_case):
    request, arguments = proof_case
    entered, release = threading.Event(), threading.Event()

    def blocked(body):
        entered.set()
        assert release.wait(3)
        return prove_publication(body, **arguments)

    endpoint = ActivationProofEndpoint(blocked)
    app = Starlette(routes=[Route("/proof", endpoint.handle, methods=["POST"])])
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            running = asyncio.create_task(client.post("/proof", json=request))
            assert await asyncio.to_thread(entered.wait, 1)
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            rejected = await client.post("/proof", json=request)
            assert rejected.status_code == 503
            assert rejected.json()["code"] == "ACK_CAPACITY_EXCEEDED"
            release.set()
    finally:
        release.set()
        endpoint.close()


@pytest.mark.anyio
async def test_proof_route_rejects_oversized_body_before_execution():
    called = []
    endpoint = ActivationProofEndpoint(lambda body: called.append(body))
    app = Starlette(routes=[Route("/proof", endpoint.handle, methods=["POST"])])
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/proof", content=b"x" * 8193)
        assert response.status_code == 400
        assert called == []
    finally:
        endpoint.close()
