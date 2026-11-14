"""The same capture attempt contract reaches MCP, REST and the registry CLI."""
# Imported fixtures are requested by the tests.
# ruff: noqa: F811

from __future__ import annotations

import json

from test_capture_attempts import authored
from test_passage_evidence import passage_tools  # noqa: F401 - shared public MCP fixture
from test_rest_api import _client

from exomem import capture_attempts, commands, writer_lease
from exomem.__main__ import main


def test_public_write_door_reserves_captures_and_settles_exact_input(vault, passage_tools):
    call, _ = passage_tools
    args = authored()
    wire = capture_attempts.CaptureWireV1.model_validate(args)
    reserved = call("manage_memory", {"operation": "sources.reserve_capture", "arguments": {"source_identity": args["source_identity"], "request_sha256": wire.sha256()}})
    ticket = reserved["reservation"]
    again = call("manage_memory", {"operation": "sources.reserve_capture", "arguments": {"source_identity": args["source_identity"], "request_sha256": wire.sha256()}})
    assert again["reservation"]["ticket"] != ticket["ticket"]
    captured = call("save_source", {"title": args["title"], "content": args["content"], "source_kind": args["source_kind"], "options": {"source_identity": args["source_identity"], "capture_reservation": ticket}, "detail": "full"})
    assert captured["capture_outcome"] == "created", captured
    settled = call("manage_memory", {"operation": "sources.settle_capture", "arguments": {"reservation": ticket}})
    assert settled["admission_closed_here"] is True and settled["past_outcome"] == "created", settled


def test_rest_routes_reservation_and_settlement_with_the_same_authentication(vault, monkeypatch):
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="test-key")
    args = authored()
    body = {"source_identity": args["source_identity"], "request_sha256": capture_attempts.CaptureWireV1.model_validate(args).sha256()}
    assert client.post("/api/reserve_source_capture", json=body).status_code == 401
    headers = {"Authorization": "Bearer test-key", "Idempotency-Key": "repeated-key"}
    reserved = client.post("/api/reserve_source_capture", json=body, headers=headers)
    assert reserved.status_code == 200, reserved.text
    ticket = reserved.json()["data"]["reservation"]
    repeated = client.post("/api/reserve_source_capture", json=body, headers=headers)
    assert repeated.json()["data"]["reservation"]["ticket"] != ticket["ticket"]
    settled = client.post("/api/settle_source_capture", json={"reservation": ticket}, headers=headers)
    assert settled.status_code == 200, settled.text
    assert settled.json()["data"]["past_outcome"] == "not_started"


def test_cli_routes_json_reservation_and_closure(vault, capsys):
    args = authored()
    assert main(["reserve_source_capture", "--source-identity", json.dumps(args["source_identity"]), "--request-sha256", capture_attempts.CaptureWireV1.model_validate(args).sha256(), "--json"]) == 0
    ticket = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["data"]["reservation"]
    assert main(["settle_source_capture", "--reservation", json.dumps(ticket), "--json"]) == 0
    settled = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["data"]
    assert settled["admission_closed_here"] is True and settled["past_outcome"] == "not_started"
    writer_lease.reset_managers_for_tests()


def test_discovery_tracks_each_required_command_independently():
    from exomem import capabilities, cell_identity

    for name, feature in (("reserve_source_capture", "capture_attempt_reservation"), ("settle_source_capture", "capture_attempt_settlement"), ("capture_source", "capture_attempt_commit")):
        command = next(c for c in commands.PRODUCT_COMMANDS if c.name == name)
        assert feature in command.mcp_meta[capabilities.SERVICE_CAPABILITIES_META_KEY]
        surface = capabilities.ActiveSurfaceDescriptor(surface="mcp", profile="product", tier2_enabled=False,
                                                        product_commands=(name,), contributing_commands=(command,))
        assert feature in cell_identity.service_contract(surface)["operations"]
