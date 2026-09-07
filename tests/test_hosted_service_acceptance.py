"""Focused executable contract for the resumable hosted acceptance runner."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/scripts/accept_hosted_service.py"


def _load():
    spec = importlib.util.spec_from_file_location("accept_hosted_service_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "oauth": {
            "authorization_server_metadata": "https://accounts.example.test/.well-known/oauth-authorization-server/api/exomem/oauth",
            "resource": "https://mcp.example.test/api/exomem/mcp/v1",
            "client_id": "hosted-acceptance",
            "redirect_uri": "http://127.0.0.1:8765/callback",
        },
        "mcp_endpoint": "https://mcp.example.test/api/exomem/mcp/v1",
        "runtime": {
            "release": "0.73.1",
            "profile": "hosted-agent",
            "contract_digest": "a" * 64,
            "runtime_image": "ghcr.io/artexis10/exomem@sha256:" + "b" * 64,
        },
        "tenants": {
            "synthetic": {"tenant_id": "hosted-synthetic", "reservation": "alpha"},
            "isolation": {"tenant_id": "hosted-isolation", "reservation": "alpha"},
        },
    }


def _write_config(tmp_path: Path, config: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config or _config()), encoding="utf-8")
    return path


def test_prepare_pins_identity_and_reserved_tenants_without_creating_resources(tmp_path: Path) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="acceptance-001"
    )

    manifest = acceptance.prepare()

    assert manifest["runtime"] == _config()["runtime"]
    assert manifest["tenants"] == _config()["tenants"]
    assert manifest["stages"]["protocol"] == {"status": "pending"}
    assert (tmp_path / "state" / "runs" / "acceptance-001" / "manifest.json").exists()
    assert "token" not in json.dumps(manifest).lower()


def test_resume_keeps_the_original_identity_tenants_and_mutation_request_id(tmp_path: Path) -> None:
    runner = _load()
    config = _write_config(tmp_path)
    first = runner.AcceptanceRunner.from_config(config, state_dir=tmp_path / "state", run_id="resume-001")
    first.prepare()
    request_id = first.mutation_request_id("durable-capture")
    first.complete_mutation("durable-capture", receipt={"status": "committed", "receipt_id": "r1"})

    resumed = runner.AcceptanceRunner.from_config(config, state_dir=tmp_path / "state", run_id="resume-001")
    manifest = resumed.prepare(resume=True)

    assert resumed.mutation_request_id("durable-capture") == request_id
    assert manifest["mutations"]["durable-capture"]["receipt"] == {"status": "committed", "receipt_id": "r1"}
    with pytest.raises(runner.AcceptanceError, match="already committed"):
        resumed.complete_mutation("durable-capture", receipt={"status": "committed", "receipt_id": "r2"})


def test_identity_drift_is_refused_before_resuming_or_cleaning_up(tmp_path: Path) -> None:
    runner = _load()
    config = _config()
    config_path = _write_config(tmp_path, config)
    acceptance = runner.AcceptanceRunner.from_config(config_path, state_dir=tmp_path / "state", run_id="identity-001")
    acceptance.prepare()
    config["runtime"]["contract_digest"] = "c" * 64
    changed = _write_config(tmp_path, config)

    with pytest.raises(runner.AcceptanceError, match="runtime identity"):
        runner.AcceptanceRunner.from_config(changed, state_dir=tmp_path / "state", run_id="identity-001").prepare(resume=True)


def test_blocked_stage_records_one_precise_operator_action_and_does_not_certify_a_host(tmp_path: Path) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="blocked-001"
    )
    acceptance.prepare()
    acceptance.block("claude-host", "approve the exact Claude OAuth consent for run blocked-001")
    acceptance.pass_stage("protocol", {"initialize": "passed", "tools_list": "passed"})

    manifest = acceptance.manifest()
    assert manifest["stages"]["claude-host"]["status"] == "blocked"
    assert manifest["stages"]["claude-host"]["operator_action"] == "approve the exact Claude OAuth consent for run blocked-001"
    assert manifest["stages"]["protocol"]["status"] == "passed"
    assert manifest["certifications"] == {}
    with pytest.raises(runner.AcceptanceError, match="already blocked"):
        acceptance.block("claude-host", "a different action")


def test_private_token_state_and_reports_redact_secrets_even_in_failures(tmp_path: Path) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="redaction-001"
    )
    acceptance.prepare()
    acceptance.save_tokens({"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_at": 100.0})
    acceptance.fail("protocol", ValueError("bearer access-secret refresh-secret"))

    report = acceptance.report()
    assert "access-secret" not in json.dumps(report)
    assert "refresh-secret" not in json.dumps(report)
    assert (tmp_path / "state" / "runs" / "redaction-001" / "tokens.json").exists()
    assert "access-secret" in (tmp_path / "state" / "runs" / "redaction-001" / "tokens.json").read_text()


def test_cleanup_only_removes_fixture_paths_explicitly_owned_by_the_matching_run(tmp_path: Path) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="cleanup-001"
    )
    acceptance.prepare()
    owned = tmp_path / "state" / "runs" / "cleanup-001" / "fixtures" / "owned.txt"
    owned.parent.mkdir(parents=True)
    owned.write_text("fixture", encoding="utf-8")
    stranger = tmp_path / "stranger.txt"
    stranger.write_text("do not remove", encoding="utf-8")
    acceptance.register_fixture(owned)

    acceptance.cleanup()

    assert not owned.exists()
    assert stranger.exists()


def test_synthetic_corpus_is_deterministic_and_meets_the_reserved_cell_floor(tmp_path: Path) -> None:
    runner = _load()
    first = runner.generate_synthetic_corpus(tmp_path / "one")
    second = runner.generate_synthetic_corpus(tmp_path / "two")

    assert first["notes"] >= 1000
    assert first["bytes"] >= 10 * 1024 * 1024
    assert first["digest"] == second["digest"]
    assert (tmp_path / "one" / "note-0000.md").read_text(encoding="utf-8") == (tmp_path / "two" / "note-0000.md").read_text(encoding="utf-8")


def test_pkce_client_validates_discovery_and_rotating_refresh_replay(tmp_path: Path) -> None:
    runner = _load()
    oauth = runner.OAuthPKCEClient(
        authorization_server_metadata="http://127.0.0.1:8765/.well-known/oauth-authorization-server/api/exomem/oauth",
        resource="http://127.0.0.1:8765/api/exomem/mcp/v1",
        client_id="hosted-acceptance",
        redirect_uri="http://127.0.0.1:8765/callback",
        allow_loopback_fixture=True,
    )
    discovery = {
        "issuer": "http://127.0.0.1:8765/api/exomem/oauth",
        "authorization_endpoint": "http://127.0.0.1:8765/api/exomem/oauth/authorize",
        "token_endpoint": "http://127.0.0.1:8765/api/exomem/oauth/token",
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["exomem.read", "exomem.write", "offline_access"],
    }

    authorization = oauth.authorization_request(discovery)
    assert authorization["code_challenge_method"] == "S256"
    assert authorization["state"] != authorization["code_verifier"]
    discovery["scopes_supported"] = ["exomem.read"]
    with pytest.raises(runner.AcceptanceError, match="hosted PKCE"):
        oauth.authorization_request(discovery)


def test_public_oauth_and_mcp_clients_use_real_isolated_loopback_protocol_fixtures() -> None:
    runner = _load()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - HTTP handler API
            if self.path != "/.well-known/oauth-authorization-server/api/exomem/oauth":
                self.send_error(404)
                return
            self._json({"issuer": base + "/api/exomem/oauth", "authorization_endpoint": base + "/api/exomem/oauth/authorize", "token_endpoint": base + "/api/exomem/oauth/token", "grant_types_supported": ["authorization_code", "refresh_token"], "code_challenge_methods_supported": ["S256"], "scopes_supported": ["exomem.read", "exomem.write", "offline_access"]})

        def do_POST(self) -> None:  # noqa: N802 - HTTP handler API
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            if self.path == "/api/exomem/oauth/token":
                parsed = urllib.parse.parse_qs(body.decode())
                assert parsed["resource"] == [base + "/api/exomem/mcp/v1"]
                if parsed["grant_type"] == ["authorization_code"]:
                    assert parsed["code_verifier"]
                    self._json({"access_token": "fixture-access", "refresh_token": "fixture-refresh", "expires_in": 900})
                elif parsed["refresh_token"] == ["fixture-refresh"]:
                    if getattr(server, "refresh_used", False):
                        self.send_error(400)
                        return
                    server.refresh_used = True
                    self._json({"access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_in": 900})
                else:
                    self.send_error(400)
                return
            assert self.path == "/api/exomem/mcp/v1"
            assert self.headers["Authorization"] == "Bearer fixture-access"
            request = json.loads(body)
            result: dict[str, Any]
            if request["method"] == "initialize":
                result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "exomem"}}
            elif request["method"] == "notifications/initialized":
                result = {}
            elif request["method"] == "tools/list":
                result = {"tools": [{"name": "remember"}, {"name": "ask_memory"}]}
            else:
                result = {"structuredContent": {"status": "committed"}}
            self._json({"jsonrpc": "2.0", "id": request["id"], "result": result})

        def _json(self, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, *_args: object) -> None:
            return None

    import urllib.parse

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        oauth = runner.OAuthPKCEClient(
            authorization_server_metadata=base + "/.well-known/oauth-authorization-server/api/exomem/oauth",
            resource=base + "/api/exomem/mcp/v1",
            client_id="fixture-client",
            redirect_uri=base + "/callback",
            allow_loopback_fixture=True,
        )
        discovery = oauth.discover()
        request = oauth.authorization_request(discovery)
        tokens = oauth.exchange_code(discovery, code="public-code", state=request["state"], expected_state=request["state"], code_verifier=request["code_verifier"])
        assert oauth.refresh(discovery, refresh_token="fixture-refresh")["refresh_token"] == "rotated-refresh"
        with pytest.raises(runner.AcceptanceError, match="token exchange failed"):
            runner.OAuthPKCEClient(authorization_server_metadata=base + "/.well-known/oauth-authorization-server/api/exomem/oauth", resource=base + "/api/exomem/mcp/v1", client_id="fixture-client", redirect_uri=base + "/callback", allow_loopback_fixture=True).refresh(discovery, refresh_token="fixture-refresh")
        mcp = runner.MCPClient(endpoint=base + "/api/exomem/mcp/v1", access_token=tokens["access_token"], allow_loopback_fixture=True)
        assert mcp.initialize()["serverInfo"]["name"] == "exomem"
        assert [tool["name"] for tool in mcp.list_tools()["tools"]] == ["remember", "ask_memory"]
        assert mcp.capture({"title": "fixture"})["structuredContent"]["status"] == "committed"
    finally:
        server.shutdown()
        thread.join()


def test_protocol_evidence_and_host_certification_are_distinct(tmp_path: Path) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="evidence-001"
    )
    acceptance.prepare()
    acceptance.record_protocol_evidence(
        initialize=True,
        tools_list=["bootstrap", "remember"],
        durable_ack={"status": "committed"},
        recall={"text": "paraphrased fact", "citation": "Knowledge Base/Notes/run.md"},
    )

    assert acceptance.manifest()["stages"]["protocol"]["status"] == "passed"
    with pytest.raises(runner.AcceptanceError, match="signed imported evidence"):
        acceptance.certify_host("claude", {"protocol": "passed"})


def test_wall_clock_windows_and_latency_summary_have_declared_semantics() -> None:
    runner = _load()
    deadline = runner.WallClockDeadline(started_at=1_000.0, duration_seconds=900.0)
    assert deadline.remaining(now=1_100.0) == 800.0
    assert deadline.remaining(now=2_000.0) == 0.0
    summary = runner.latency_summary([1.0, 2.0, 3.0, 4.0, 100.0], errors=1, kind="warm")
    assert summary == {"kind": "warm", "samples": 5, "p50_ms": 3.0, "p95_ms": 100.0, "errors": 1}
    with pytest.raises(runner.AcceptanceError, match="100 warm"):
        runner.validate_benchmark_samples({"initialize": [1.0] * 99}, cold_runs=20)
    with pytest.raises(runner.AcceptanceError, match="missing warm samples"):
        runner.validate_benchmark_samples({}, cold_runs=20)


def test_hosted_oauth_metadata_uses_the_canonical_resource_contract() -> None:
    runner = _load()
    client = runner.OAuthPKCEClient(
        authorization_server_metadata="http://127.0.0.1:8765/.well-known/oauth-authorization-server/api/exomem/oauth",
        resource="http://127.0.0.1:8765/api/exomem/mcp/v1",
        client_id="fixture-client",
        redirect_uri="http://127.0.0.1:8765/callback",
        allow_loopback_fixture=True,
    )
    metadata = {
        "issuer": "http://127.0.0.1:8765/api/exomem/oauth",
        "authorization_endpoint": "http://127.0.0.1:8765/api/exomem/oauth/authorize",
        "token_endpoint": "http://127.0.0.1:8765/api/exomem/oauth/token",
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["exomem.read", "exomem.write", "offline_access"],
    }

    request = client.authorization_request(metadata)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request["url"]).query)

    assert query["resource"] == ["http://127.0.0.1:8765/api/exomem/mcp/v1"]
    assert query["scope"] == ["exomem.read exomem.write offline_access"]
    assert "audience" not in query


def test_blocked_checkpoints_are_idempotent_and_can_resume_to_pass(tmp_path: Path) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="resume-blocked-001"
    )
    acceptance.prepare()
    action = "complete exact operator consent"
    acceptance.block("restore", action)
    acceptance.block("restore", action)
    acceptance.pass_stage("restore", {"isolated": "restored"})

    assert acceptance.manifest()["stages"]["restore"] == {
        "status": "passed",
        "evidence": {"isolated": "restored"},
    }


def test_cli_runs_producer_shaped_initialize_capture_and_fresh_cited_recall(tmp_path: Path) -> None:
    runner = _load()
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"method": request["method"], "headers": dict(self.headers)})
            method = request["method"]
            if method == "initialize":
                result: dict[str, Any] = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "Hosted Exomem"}}
            elif method == "tools/list":
                assert self.headers["Mcp-Protocol-Version"] == "2025-06-18"
                result = {"tools": [{"name": "remember"}, {"name": "ask_memory"}, {"name": "read_memory"}]}
            elif method == "notifications/initialized":
                result = {}
            else:
                tool = request["params"]["name"]
                if tool == "remember":
                    result = {"structuredContent": {"result": {"outcome": "committed"}}}
                elif tool == "ask_memory":
                    result = {"structuredContent": {"result": {"hits": [{"path": "Knowledge Base/Notes/Insights/acceptance.md", "text": "hosted acceptance sentinel cli-run-001"}]}}}
                else:
                    result = {"structuredContent": {"result": {"content": "hosted acceptance sentinel cli-run-001"}}}
            envelope = {"jsonrpc": "2.0", "id": request["id"], "result": result}
            sse = method == "tools/call" and request["params"]["name"] == "ask_memory"
            body = (f"data: {json.dumps(envelope)}\n\n" if sse else json.dumps(envelope)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        config = _config()
        config["mcp_endpoint"] = base + "/api/exomem/mcp/v1"
        config["oauth"]["resource"] = config["mcp_endpoint"]
        config["oauth"]["authorization_server_metadata"] = base + "/.well-known/oauth-authorization-server/api/exomem/oauth"
        config_path = _write_config(tmp_path, config)
        acceptance = runner.AcceptanceRunner.from_config(config_path, state_dir=tmp_path / "state", run_id="cli-run-001", allow_loopback_fixture=True)
        acceptance.prepare()
        for tenant in ("synthetic", "isolation"):
            acceptance.save_tokens({"access_token": f"{tenant}-access", "refresh_token": f"{tenant}-refresh"}, tenant=tenant)

        assert runner.main(["run", "--config", str(config_path), "--state-dir", str(tmp_path / "state"), "--run-id", "cli-run-001", "--resume"], allow_loopback_fixture=True) == 0
        assert acceptance.manifest()["stages"]["protocol"]["status"] == "passed"
        assert [request["method"] for request in requests] == ["initialize", "notifications/initialized", "tools/list", "tools/call", "initialize", "notifications/initialized", "tools/call", "tools/call"]
    finally:
        server.shutdown()
        thread.join()
