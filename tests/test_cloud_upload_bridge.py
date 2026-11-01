"""A Cloud cell holds agent bytes only for the login the gateway proved (bridge-cloud-agent-uploads).

The gateway here is a stand-in that signs the same version-1 objects as the
Substrate gateway. Every request goes through the cell's real HTTP app, so the
bearer check, origin binding, authorization middleware and output filter all run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator
from starlette.middleware import Middleware

from exomem import cloud_origin, server
from exomem.__main__ import _attach_main

CELL_ID = "cellabcdefgh2345"
TOKEN = "cloud-cell-bearer-for-upload-tests-0001"  # noqa: S105 - test fixture
RESOURCE = "https://cloud.example/mcp/v1"
UPLOAD_URL = "https://cloud.example/upload"
LOGIN_A = {
    "tenant_id": "6f1c1b7e-2a4b-4c55-9d8e-0a1b2c3d4e5f",
    "cell_id": CELL_ID,
    "client_id": "https://client.example/oauth/metadata.json",
    "grant_id": "1d4f6a2e-5b3c-4e7d-8f9a-0b1c2d3e4f5a",
    "family_id": "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d",
    "resource": RESOURCE,
}
# The same app on the same tenant after another consent: a different grant and login family.
LOGIN_B = {**LOGIN_A, "grant_id": "2e5a7b3f-6c4d-4f8e-9a0b-1c2d3e4f5a6b",
           "family_id": "8b9c0d1e-2f3a-4b5c-9d6e-7f8a9b0c1d2e"}
ORIGINAL = b"\xef\xbb\xbf00:18:23 Speaker A: an exact original.\r\n\x00\xff"


def _sign(key, claims, audience, lifetime):
    now = int(time.time())
    return jwt.encode(
        {"v": 1, **claims, "iss": claims["resource"], "aud": audience, "iat": now,
         "exp": now + lifetime, "jti": str(uuid.uuid4())},
        key, algorithm="EdDSA", headers={"typ": "JWT"},
    )


def _mcp_origin(key, login):
    """The two headers the gateway adds to every MCP request it forwards to a selected cell."""
    return {
        "x-exomem-cloud-origin": _sign(key, {**login, "purpose": "mcp"}, "exomem-cloud-origin", 60),
        "x-exomem-cloud-upload-route": _sign(
            key, {**login, "upload_url": UPLOAD_URL}, "exomem-cloud-upload-route", 900
        ),
    }


def _upload_origin(key, login, ticket):
    """What the gateway's upload route sends the cell after rechecking the login."""
    return {
        "authorization": f"Bearer {TOKEN}",
        "x-exomem-cloud-upload-ticket": ticket,
        "x-exomem-cloud-origin": _sign(
            key, {**login, "purpose": "upload", "ticket_sha256": hashlib.sha256(ticket.encode()).hexdigest()},
            "exomem-cloud-origin", 60,
        ),
    }


def _configure_cell(monkeypatch):
    key = Ed25519PrivateKey.generate()
    monkeypatch.setenv("EXOMEM_CLOUD_UPLOAD_PUBLIC_KEY", key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode())
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL_ID)
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", TOKEN)
    monkeypatch.delenv("EXOMEM_CLOUD_CELL_TOKEN_PREVIOUS", raising=False)
    monkeypatch.delenv("EXOMEM_CLOUD_READ_ONLY", raising=False)
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    monkeypatch.setattr(server, "load_dotenv", lambda *args, **kwargs: None)
    return key


@pytest.fixture
def cell(vault, monkeypatch):
    key = _configure_cell(monkeypatch)
    mcp = server.build_server(require_auth=True)
    app = mcp.http_app(stateless_http=True, json_response=True, middleware=server.http_middleware(mcp))
    try:
        yield key, app, vault
    finally:
        mcp._exomem_local_runtime_activation._shutdown.set()


@pytest.fixture
def enforcing_cell(vault, monkeypatch):
    """The same cell with the proof gate in enforce mode, selected by the middleware's parameter."""
    key = _configure_cell(monkeypatch)
    mcp = server.build_server(require_auth=True)
    middleware = [entry for entry in server.http_middleware(mcp) if entry.cls is not cloud_origin.CloudOriginMiddleware]
    middleware.append(Middleware(cloud_origin.CloudOriginMiddleware, enforce=True))
    app = mcp.http_app(stateless_http=True, json_response=True, middleware=middleware)
    try:
        yield key, app
    finally:
        mcp._exomem_local_runtime_activation._shutdown.set()


def _run(app, scenario):
    async def wrapped():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
            ) as client:
                return await scenario(client)
    return asyncio.run(wrapped())


async def _tool(client, name, arguments, origin):
    response = await client.post(
        "/mcp",
        headers={"accept": "application/json, text/event-stream", "content-type": "application/json",
                 "authorization": f"Bearer {TOKEN}", "mcp-protocol-version": "2025-11-25", **origin},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    payload = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    return payload.get("result", payload) if payload.get("success", True) else payload


def _prepare_arguments():
    return {"operation": "upload", "lane": "source", "hold": True,
            "expected_sha256": hashlib.sha256(ORIGINAL).hexdigest()}


async def _hold(client, origin, ticket):
    return await client.post(
        "/upload", headers=origin,
        files={"file": ("original.txt", ORIGINAL, "text/plain")}, data={"hold": "1", "lane": "source"},
    )


def _ticket(descriptor):
    return "exomem-upload:" + cloud_origin.envelope_nonce(descriptor["token"])


class _Gateway(httpx.BaseTransport):
    """The gateway's /upload relay: verify the capsule, then forward with fresh origin."""

    def __init__(self, key, app):
        self.key, self.app, self.capsules = key, app, []

    def handle_request(self, request):
        assert str(request.url) == UPLOAD_URL
        token = request.headers["authorization"].removeprefix("Bearer ")
        capsule = bytes.fromhex(token.removeprefix(cloud_origin.TOKEN_PREFIX).split(":")[0]).decode()
        route = jwt.decode(capsule, self.key.public_key(), algorithms=["EdDSA"],
                           audience="exomem-cloud-upload-route")
        self.capsules.append(route)
        login = {name: route[name] for name in LOGIN_A}
        headers = {**_upload_origin(self.key, login, "exomem-upload:" + cloud_origin.envelope_nonce(token)),
                   "content-type": request.headers["content-type"]}
        body = request.read()

        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app)) as client:
                return await client.post("http://cell.local/upload", headers=headers, content=body)
        response = asyncio.run(send())
        return httpx.Response(response.status_code, headers={"content-type": "application/json"},
                              content=response.content)


def test_agent_handoff_cli_upload_and_capture_keep_exact_bytes_for_one_login(cell, tmp_path, capsys):
    key, app, vault = cell
    digest = hashlib.sha256(ORIGINAL).hexdigest()

    async def prepare(client):
        listed = await client.post(
            "/mcp", headers={"accept": "application/json, text/event-stream", "content-type": "application/json",
                             "authorization": f"Bearer {TOKEN}", "mcp-protocol-version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        tools = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
        assert "transfer_artifact" in tools
        media = Draft202012Validator(tools["read_media"]["inputSchema"])
        assert media.is_valid({"operation": {"kind": "status"}})
        assert not media.is_valid({"operation": {"kind": "video", "path": "sample.mp4"}})
        return await _tool(client, "transfer_artifact", _prepare_arguments(), _mcp_origin(key, LOGIN_A))

    # The descriptor the agent saves has passed the cell's real output filter.
    descriptor = _run(app, prepare)
    assert set(descriptor) == {"token", "upload_url", "ttl_seconds", "hold", "lane", "expected_sha256",
                               "max_bytes", "handoff_status", "committed"}
    assert descriptor["upload_url"] == UPLOAD_URL and descriptor["committed"] is False
    assert 0 < descriptor["ttl_seconds"] <= 900
    (tmp_path / "handoff.json").write_text(json.dumps(descriptor))
    (tmp_path / "original.bin").write_bytes(ORIGINAL)
    gateway = _Gateway(key, app)
    assert _attach_main(
        ["original.bin", "--handoff-file", str(tmp_path / "handoff.json"), "--root", str(tmp_path)],
        transport=gateway,
    ) == 0
    assert [{name: route[name] for name in LOGIN_A} for route in gateway.capsules] == [LOGIN_A]
    held = json.loads(capsys.readouterr().out)
    capture = {"title": "Exact meeting original", "source_kind": "session",
               "files": [held["file"]], "expected_sha256": digest}

    async def capture_twice(client):
        # Its own title: one client's identical retries share a terminal result across logins.
        other = await _tool(client, "capture_source", {**capture, "title": "Another login's attempt"},
                            _mcp_origin(key, LOGIN_B))
        owned = await _tool(client, "capture_source", capture, _mcp_origin(key, LOGIN_A))
        return other, owned

    other, owned = _run(app, capture_twice)
    [refused] = other["files"]
    assert refused["code"] == "HELD_UPLOAD_UNAVAILABLE" and other["summary"]["stored"] == 0
    [receipt] = owned["files"]
    assert receipt["outcome"] == "stored" and receipt["hash"] == digest
    assert (vault / receipt["stored_path"]).read_bytes() == ORIGINAL


def test_another_logins_proof_cannot_spend_a_ticket(cell):
    key, app, _ = cell

    async def scenario(client):
        ticket = _ticket(await _tool(client, "transfer_artifact", _prepare_arguments(), _mcp_origin(key, LOGIN_A)))
        crossed = await _hold(client, _upload_origin(key, LOGIN_B, ticket), ticket)
        owned = await _hold(client, _upload_origin(key, LOGIN_A, ticket), ticket)
        replayed = await _hold(client, _upload_origin(key, LOGIN_A, ticket), ticket)
        return crossed, owned, replayed

    crossed, owned, replayed = _run(app, scenario)
    assert crossed.status_code == 401 and crossed.json()["code"] == "UPLOAD_HANDOFF_UNAVAILABLE"
    # The refusal came before the claim: the issuing login still spends the ticket, once.
    assert owned.status_code == 201 and owned.json()["state"] == "held"
    assert replayed.status_code == 401


@pytest.mark.parametrize("refusal", ["cell_bearer", "other_ticket", "mcp_proof", "read_only"])
def test_receiver_refuses_before_claiming_the_ticket(cell, monkeypatch, refusal):
    key, app, _ = cell

    async def scenario(client):
        ticket = _ticket(await _tool(client, "transfer_artifact", _prepare_arguments(), _mcp_origin(key, LOGIN_A)))
        headers = _upload_origin(key, LOGIN_A, ticket)
        if refusal == "cell_bearer":
            headers["authorization"] = "Bearer exomem-upload:" + ticket.split(":")[1]
        elif refusal == "other_ticket":
            headers["x-exomem-cloud-origin"] = _upload_origin(key, LOGIN_A, "exomem-upload:" + "0" * 64)[
                "x-exomem-cloud-origin"]
        elif refusal == "mcp_proof":
            headers["x-exomem-cloud-origin"] = _mcp_origin(key, LOGIN_A)["x-exomem-cloud-origin"]
        else:
            monkeypatch.setenv("EXOMEM_CLOUD_READ_ONLY", "1")
        refused = await _hold(client, headers, ticket)
        monkeypatch.delenv("EXOMEM_CLOUD_READ_ONLY", raising=False)
        accepted = await _hold(client, _upload_origin(key, LOGIN_A, ticket), ticket)
        return refused, accepted

    refused, accepted = _run(app, scenario)
    assert refused.status_code == (503 if refusal == "read_only" else 401)
    assert accepted.status_code == 201


def test_handoff_needs_the_route_capsule_for_the_same_login(cell):
    key, app, _ = cell
    crossed = {**_mcp_origin(key, LOGIN_A),
               "x-exomem-cloud-upload-route": _mcp_origin(key, LOGIN_B)["x-exomem-cloud-upload-route"]}

    async def scenario(client):
        return (await _tool(client, "transfer_artifact", _prepare_arguments(), crossed),
                await _tool(client, "transfer_artifact", {"operation": "download", "artifact": "x.pdf"},
                            _mcp_origin(key, LOGIN_A)))

    unverified, download = _run(app, scenario)
    assert "no verified gateway origin" in json.dumps(unverified)
    # A download of a path that is no readable original is refused without saying whether it exists.
    assert "ORIGINAL_UNAVAILABLE" in json.dumps(download)


@pytest.mark.parametrize("change", ["expired", "route_audience", "other_cell", "long_life", "extra_claim",
                                    "other_key", "issuer"])
def test_origin_proof_rejects_each_altered_field(cell, change):
    key, _, _ = cell
    login, audience, lifetime, signer, extra = dict(LOGIN_A), "exomem-cloud-origin", 60, key, {}
    if change == "route_audience":
        audience = "exomem-cloud-upload-route"
    elif change == "other_cell":
        login["cell_id"] = "cellzzzzzzzz2345"
    elif change == "long_life":
        lifetime = 61
    elif change == "extra_claim":
        extra = {"scope": "exomem.write"}
    elif change == "other_key":
        signer = Ed25519PrivateKey.generate()
    proof = _sign(signer, {**login, "purpose": "mcp", **extra}, audience, lifetime)
    if change == "expired":
        proof = _sign(key, {**login, "purpose": "mcp"}, audience, 60)
        claims = jwt.decode(proof, options={"verify_signature": False})
        proof = jwt.encode({**claims, "iat": claims["iat"] - 120, "exp": claims["exp"] - 120}, key,
                           algorithm="EdDSA", headers={"typ": "JWT"})
    if change == "issuer":
        claims = jwt.decode(proof, options={"verify_signature": False})
        proof = jwt.encode({**claims, "iss": "https://elsewhere.example/mcp"}, key, algorithm="EdDSA",
                           headers={"typ": "JWT"})
    headers = {**_mcp_origin(key, LOGIN_A), "x-exomem-cloud-origin": proof}
    assert cloud_origin.verify_request(headers) is None
    assert cloud_origin.verify_request(_mcp_origin(key, LOGIN_A)) is not None


@pytest.mark.parametrize("token", [
    cloud_origin.TOKEN_PREFIX + "6579" + "4A" + ":" + "0" * 64,
    cloud_origin.TOKEN_PREFIX + "65794a:" + "0" * 63,
    cloud_origin.TOKEN_PREFIX + "65794a" + ":" + "0" * 64 + ":extra",
    cloud_origin.TOKEN_PREFIX + "ff" + ":" + "0" * 64,
    cloud_origin.TOKEN_PREFIX + "61" * 4096 + ":" + "0" * 64,
])
def test_cli_refuses_an_altered_cloud_token_before_sending(tmp_path, token):
    (tmp_path / "original.bin").write_bytes(ORIGINAL)
    (tmp_path / "handoff.json").write_text(json.dumps({
        "token": token, "upload_url": UPLOAD_URL, "ttl_seconds": 900, "hold": True, "lane": "source",
        "expected_sha256": hashlib.sha256(ORIGINAL).hexdigest(), "max_bytes": 1024,
        "handoff_status": "handoff_prepared", "committed": False,
    }))

    def never_send(request):
        pytest.fail("an altered token reached the network")
    assert _attach_main(
        ["original.bin", "--handoff-file", str(tmp_path / "handoff.json"), "--root", str(tmp_path)],
        transport=httpx.MockTransport(never_send),
    ) == 2


# Report mode (contain-cloud-controller-compromise D1): every MCP request logs the credential it used and is
# served exactly as before. The proof gate itself only refuses when the middleware is told to enforce.
PROOF_GATE_EVENT = "cloud_request_credential"


async def _list_tools(client, *, bearer=True, headers=None):
    return await client.post(
        "/mcp",
        headers={"accept": "application/json, text/event-stream", "content-type": "application/json",
                 "mcp-protocol-version": "2025-11-25", **({"authorization": f"Bearer {TOKEN}"} if bearer else {}),
                 **(headers or {})},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )


def _credential_events(caplog):
    return [record for record in caplog.records if getattr(record, "event", None) == PROOF_GATE_EVENT]


def _one_credential_event(caplog):
    (record,) = _credential_events(caplog)
    return record.fields


def test_report_mode_serves_a_bearer_alone_and_logs_it(cell, caplog):
    _key, app, _vault = cell
    caplog.set_level(logging.INFO)

    response = _run(app, lambda client: _list_tools(client))

    assert response.status_code == 200
    assert _one_credential_event(caplog) == {
        "credential": "bearer_only", "proof": "absent", "verdict": "would_refuse", "cell_id": CELL_ID}


def test_report_mode_logs_a_bearer_with_a_valid_proof_as_both(cell, caplog):
    key, app, _vault = cell
    caplog.set_level(logging.INFO)

    response = _run(app, lambda client: _list_tools(client, headers=_mcp_origin(key, LOGIN_A)))

    assert response.status_code == 200
    assert _one_credential_event(caplog) == {
        "credential": "both", "proof": "valid", "verdict": "pass", "cell_id": CELL_ID}


def test_report_mode_serves_a_forged_proof_as_bearer_only_and_names_the_proof_invalid(cell, caplog):
    _key, app, _vault = cell
    caplog.set_level(logging.INFO)
    forger = Ed25519PrivateKey.generate()

    response = _run(app, lambda client: _list_tools(client, headers=_mcp_origin(forger, LOGIN_A)))

    assert response.status_code == 200
    assert _one_credential_event(caplog) == {
        "credential": "bearer_only", "proof": "invalid", "verdict": "would_refuse", "cell_id": CELL_ID}


def test_report_mode_still_refuses_a_request_without_a_bearer(cell, caplog):
    key, app, _vault = cell
    caplog.set_level(logging.INFO)

    proof_only = _run(app, lambda client: _list_tools(client, bearer=False, headers=_mcp_origin(key, LOGIN_A)))
    neither = _run(app, lambda client: _list_tools(client, bearer=False))

    assert (proof_only.status_code, neither.status_code) == (401, 401)
    assert [record.fields["credential"] for record in _credential_events(caplog)] == ["proof_only", "neither"]


def test_report_mode_without_a_gateway_key_logs_the_proof_as_unconfigured(cell, monkeypatch, caplog):
    _key, app, _vault = cell
    monkeypatch.delenv("EXOMEM_CLOUD_UPLOAD_PUBLIC_KEY")
    caplog.set_level(logging.INFO)

    response = _run(app, lambda client: _list_tools(client))

    assert response.status_code == 200
    assert _one_credential_event(caplog) == {
        "credential": "proof_unconfigured", "proof": "unconfigured", "verdict": "would_refuse", "cell_id": CELL_ID}


def test_credential_event_carries_no_token_or_claim(cell, caplog):
    key, app, _vault = cell
    caplog.set_level(logging.INFO)
    headers = _mcp_origin(key, LOGIN_A)

    _run(app, lambda client: _list_tools(client, headers=headers))

    text = json.dumps(vars(_credential_events(caplog)[0]), default=str)
    assert not [secret for secret in (TOKEN, *headers.values(), LOGIN_A["tenant_id"], LOGIN_A["client_id"])
                if secret in text]


def test_health_routes_are_not_gated_or_logged(cell, caplog):
    _key, app, _vault = cell
    caplog.set_level(logging.INFO)

    async def probe(client):
        return await client.get("/health")

    assert _run(app, probe).status_code == 200
    assert _credential_events(caplog) == []


def test_enforce_mode_refuses_a_bearer_alone_and_serves_a_valid_proof(enforcing_cell, caplog):
    key, app = enforcing_cell
    caplog.set_level(logging.INFO)

    bearer_only = _run(app, lambda client: _list_tools(client))
    forged = _run(app, lambda client: _list_tools(client, headers=_mcp_origin(Ed25519PrivateKey.generate(), LOGIN_A)))
    both = _run(app, lambda client: _list_tools(client, headers=_mcp_origin(key, LOGIN_A)))

    assert (bearer_only.status_code, forged.status_code, both.status_code) == (401, 401, 200)
    assert [record.fields["verdict"] for record in _credential_events(caplog)] == ["refused", "refused", "pass"]


def test_enforce_mode_without_a_gateway_key_refuses_every_mcp_request_and_keeps_health(enforcing_cell, monkeypatch):
    _key, app = enforcing_cell
    monkeypatch.delenv("EXOMEM_CLOUD_UPLOAD_PUBLIC_KEY")

    async def scenario(client):
        return await _list_tools(client), await client.get("/health")

    mcp, health = _run(app, scenario)

    assert (mcp.status_code, health.status_code) == (401, 200)
