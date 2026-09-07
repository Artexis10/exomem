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

from exomem import commands, semantic_units, writer_lease

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


def test_synthetic_corpus_notes_are_valid_compiled_compact_observations(tmp_path: Path) -> None:
    runner = _load()
    runner.generate_synthetic_corpus(tmp_path / "corpus")

    categories: set[str] = set()
    for note in (tmp_path / "corpus").glob("note-*.md"):
        content = note.read_text(encoding="utf-8")
        parsed = semantic_units.parse_semantic_units(content, path=str(note))
        assert parsed.errors == ()
        assert "\n## Observations\n" in content
        assert len(parsed.units) == 1
        assert parsed.units[0].form == "compact"
        assert parsed.units[0].kind == "observation"
        assert parsed.units[0].content
        categories.add(parsed.units[0].category)
    assert len(categories) >= 8


def test_seed_and_benchmark_refuse_a_tampered_generated_corpus_before_side_effects(tmp_path: Path) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="fixture-tamper-001"
    )
    acceptance.prepare()
    corpus = runner.generate_synthetic_corpus(acceptance.run_dir / "fixtures" / "corpus")
    acceptance.register_fixture(acceptance.run_dir / "fixtures" / "corpus")
    manifest = acceptance.manifest()
    manifest["stages"]["performance"] = {"status": "pending", "fixture": corpus}
    acceptance._write_manifest(manifest)
    (acceptance.run_dir / "fixtures" / "corpus" / "note-0000.md").write_text("truncated\n", encoding="utf-8")

    with pytest.raises(runner.AcceptanceError, match="corpus fixture"):
        acceptance.seed_corpus()
    with pytest.raises(runner.AcceptanceError, match="corpus fixture"):
        acceptance.run_benchmark()


def test_seed_refuses_tampered_fixture_before_resuming_committed_cells(tmp_path: Path) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="fixture-resume-001"
    )
    acceptance.prepare()
    corpus = runner.generate_synthetic_corpus(acceptance.run_dir / "fixtures" / "corpus")
    manifest = acceptance.manifest()
    manifest["stages"]["performance"] = {
        "status": "pending",
        "fixture": corpus,
        "cell_corpus": {
            tenant: {"status": "committed", "convergence": "passed"}
            for tenant in ("synthetic", "isolation")
        },
    }
    acceptance._write_manifest(manifest)
    (acceptance.run_dir / "fixtures" / "corpus" / "note-0999.md").write_text("truncated\n", encoding="utf-8")

    with pytest.raises(runner.AcceptanceError, match="corpus fixture"):
        acceptance.seed_corpus()


def test_tenant_sentinels_are_stable_distinct_and_non_overlapping() -> None:
    runner = _load()

    synthetic = runner.tenant_sentinel("sentinel-001", "synthetic")
    isolation = runner.tenant_sentinel("sentinel-001", "isolation")

    assert synthetic == runner.tenant_sentinel("sentinel-001", "synthetic")
    assert isolation == runner.tenant_sentinel("sentinel-001", "isolation")
    assert synthetic != isolation
    assert synthetic not in isolation
    assert isolation not in synthetic


@pytest.mark.parametrize(
    ("result", "committed"),
    [
        (
            {
                "structuredContent": {
                    "result": {
                        "ok": True,
                        "state": "committed",
                        "terminal": True,
                        "status": "committed",
                        "mutated": True,
                    }
                }
            },
            True,
        ),
        (
            {
                "structuredContent": {
                    "ok": True,
                    "state": "committed",
                    "terminal": True,
                    "status": "committed",
                    "mutated": True,
                }
            },
            True,
        ),
        ({"isError": True, "structuredContent": {"result": {"ok": True, "state": "committed", "terminal": True, "status": "committed", "mutated": True}}}, False),
        ({"structuredContent": {"result": {"ok": True, "state": "pending", "terminal": True, "status": "committed", "mutated": True}}}, False),
        ({"structuredContent": {"result": {"ok": True, "state": "committed", "terminal": False, "status": "committed", "mutated": True}}}, False),
        ({"structuredContent": {"result": {"ok": True, "state": "committed", "terminal": True, "status": "committed", "mutated": False}}}, False),
        ({"structuredContent": {"result": {"status": "failed", "message": "nothing committed"}}}, False),
        ({"structuredContent": {"result": {"message": "committed"}}}, False),
    ],
)
def test_durable_acknowledgement_requires_canonical_committed_terminal(result: dict[str, Any], committed: bool) -> None:
    runner = _load()

    assert runner.committed_tool_receipt(result) is committed


def test_tool_results_marked_as_errors_are_not_usable_as_recall_evidence() -> None:
    runner = _load()

    with pytest.raises(runner.AcceptanceError, match="unsuccessful"):
        runner._tool_result({"isError": True, "structuredContent": {"result": {"hits": [{"path": "Knowledge Base/Notes/Insights/sentinel.md", "text": "forged"}]}}})


def test_remember_review_journal_replays_the_exact_prepared_commit_after_lost_ack(
    tmp_path: Path,
) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="review-journal-001"
    )
    acceptance.prepare()
    calls: list[tuple[dict[str, Any], str | None]] = []

    class Client:
        interrupted = True

        def capture(
            self, arguments: dict[str, Any], *, idempotency_key: str | None = None
        ) -> dict[str, Any]:
            calls.append((dict(arguments), idempotency_key))
            if arguments.get("validate_only") is True:
                return {
                    "structuredContent": {
                        "state": "needs_review",
                        "diagnostics": {
                            "draft_id": "draft-001",
                            "draft_hash": "a" * 64,
                            "draft_token": "draft-token-001",
                            "relation_review_hash": "a" * 64,
                            "reviewed_none_required": True,
                            "has_non_review_blockers": False,
                            "committable_after_review": True,
                        },
                    }
                }
            if self.interrupted:
                self.interrupted = False
                raise runner.AcceptanceError("lost terminal acknowledgement")
            return {
                "structuredContent": {
                    "ok": True,
                    "state": "committed",
                    "terminal": True,
                    "status": "committed",
                    "mutated": True,
                    "idempotency_key": "gateway-derived-key",
                }
            }

    arguments = {
        "title": "Journalled reviewed write",
        "content": "## Observations\n- [acceptance] preserve exact prepared commit.\n",
        "note_type": "insight",
        "sources": [],
    }
    client = Client()
    with pytest.raises(runner.AcceptanceError, match="lost terminal"):
        acceptance.remember_with_review(
            client,
            mutation="protocol-reviewed-write",
            arguments=arguments,
            idempotency_key="stable-write-key",
        )

    journal = json.loads(
        (acceptance.run_dir / "mutations" / "protocol-reviewed-write.json").read_text(
            encoding="utf-8"
        )
    )
    assert journal["status"] == "prepared"
    assert journal["arguments"]["draft_token"] == "draft-token-001"
    assert journal["arguments"]["relation_disposition"] == "reviewed_none"
    assert "validate_only" not in journal["arguments"]
    assert calls[0][1] is None
    assert calls[0][0]["validate_only"] is True

    tampered = json.loads(json.dumps(journal))
    tampered["arguments"]["title"] = "Unexpected replacement payload"
    journal_path = acceptance.run_dir / "mutations" / "protocol-reviewed-write.json"
    journal_path.write_bytes(runner.canonical_json(tampered))
    resumed = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="review-journal-001"
    )
    resumed.prepare(resume=True)
    with pytest.raises(runner.AcceptanceError, match="prepared mutation journal"):
        resumed.remember_with_review(
            client,
            mutation="protocol-reviewed-write",
            arguments=arguments,
            idempotency_key="stable-write-key",
        )
    journal_path.write_bytes(runner.canonical_json(journal))

    terminal = resumed.remember_with_review(
        client,
        mutation="protocol-reviewed-write",
        arguments=arguments,
        idempotency_key="stable-write-key",
    )

    assert terminal["state"] == "committed"
    assert terminal["idempotency_key"] == "gateway-derived-key"
    assert len(calls) == 3
    assert calls[1] == calls[2]
    assert calls[1][1] == "stable-write-key"
    assert json.loads(
        journal_path.read_text(encoding="utf-8")
    )["status"] == "confirmed"
    confirmed = json.loads(journal_path.read_text(encoding="utf-8"))
    assert "acknowledgement_sha256" in confirmed
    reused = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="review-journal-001"
    )
    reused.prepare(resume=True)
    assert reused.remember_with_review(
        client,
        mutation="protocol-reviewed-write",
        arguments=arguments,
        idempotency_key="stable-write-key",
    ) == terminal
    assert len(calls) == 3
    confirmed["arguments"]["title"] = "Unexpected confirmed payload"
    journal_path.write_bytes(runner.canonical_json(confirmed))
    with pytest.raises(runner.AcceptanceError, match="prepared mutation journal"):
        resumed.remember_with_review(
            client,
            mutation="protocol-reviewed-write",
            arguments=arguments,
            idempotency_key="stable-write-key",
        )
    confirmed["arguments"]["title"] = arguments["title"]
    confirmed["terminal"]["idempotency_key"] = "swapped-gateway-key"
    journal_path.write_bytes(runner.canonical_json(confirmed))
    with pytest.raises(runner.AcceptanceError, match="confirmed mutation journal"):
        resumed.remember_with_review(
            client,
            mutation="protocol-reviewed-write",
            arguments=arguments,
            idempotency_key="stable-write-key",
        )
    confirmed["terminal"] = None
    journal_path.write_bytes(runner.canonical_json(confirmed))
    with pytest.raises(runner.AcceptanceError, match="confirmed mutation journal"):
        resumed.remember_with_review(
            client,
            mutation="protocol-reviewed-write",
            arguments=arguments,
            idempotency_key="stable-write-key",
        )


def test_remember_review_rejects_unknown_validation_without_preparing_a_commit(
    tmp_path: Path,
) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="review-refusal-001"
    )
    acceptance.prepare()

    class Client:
        def capture(
            self, arguments: dict[str, Any], *, idempotency_key: str | None = None
        ) -> dict[str, Any]:
            assert idempotency_key is None
            assert arguments["validate_only"] is True
            return {
                "structuredContent": {
                    "state": "needs_review",
                    "diagnostics": {
                        "draft_id": "draft-refused",
                        "draft_hash": "a" * 64,
                        "draft_token": "draft-token-refused",
                        "relation_review_hash": "a" * 64,
                        "reviewed_none_required": True,
                        "has_non_review_blockers": True,
                        "committable_after_review": True,
                    },
                }
            }

    with pytest.raises(runner.AcceptanceError, match="validation is not a reviewable"):
        acceptance.remember_with_review(
            Client(),
            mutation="refused-write",
            arguments={
                "title": "Refused reviewed write",
                "content": "## Observations\n- [acceptance] reject unknown diagnostic.\n",
                "note_type": "insight",
                "sources": [],
            },
            idempotency_key="stable-refusal-key",
        )
    assert not (acceptance.run_dir / "mutations" / "refused-write.json").exists()


def test_remember_review_commits_an_explicitly_committable_non_review_draft(
    tmp_path: Path,
) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="review-free-001"
    )
    acceptance.prepare()
    commits: list[dict[str, Any]] = []

    class Client:
        def capture(
            self, arguments: dict[str, Any], *, idempotency_key: str | None = None
        ) -> dict[str, Any]:
            if arguments.get("validate_only") is True:
                assert idempotency_key is None
                return {
                    "structuredContent": {
                        "state": "needs_review",
                        "diagnostics": {
                            "draft_id": "draft-free",
                            "draft_hash": "c" * 64,
                            "draft_token": "draft-token-free",
                            "reviewed_none_required": False,
                            "has_non_review_blockers": False,
                            "committable_without_review": True,
                        },
                    }
                }
            commits.append(dict(arguments))
            return {
                "structuredContent": {
                    "ok": True,
                    "state": "committed",
                    "terminal": True,
                    "status": "committed",
                    "mutated": True,
                }
            }

    acceptance.remember_with_review(
        Client(),
        mutation="direct-reviewed-write",
        arguments={
            "title": "Direct committable write",
            "content": "## Observations\n- [acceptance] commit no-review draft.\n",
            "note_type": "insight",
            "sources": [],
        },
        idempotency_key="stable-direct-key",
    )

    assert "relation_disposition" not in commits[0]
    assert commits[0]["draft_id"] == "draft-free"

def test_remember_review_uses_the_real_public_writer_two_phase_contract(
    tmp_path: Path, vault: Path
) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="real-review-001"
    )
    acceptance.prepare()
    command = next(item for item in commands.PRODUCT_COMMANDS if item.name == "remember")
    manager = writer_lease.LeaseManager(
        writer_lease.LeaseConfig(state_dir=tmp_path / "writer-state")
    )
    calls: list[tuple[dict[str, Any], str | None]] = []

    class Client:
        def capture(
            self, arguments: dict[str, Any], *, idempotency_key: str | None = None
        ) -> dict[str, Any]:
            calls.append((dict(arguments), idempotency_key))
            return {
                "structuredContent": manager.invoke(
                    command, (vault,), arguments, idempotency_key=idempotency_key
                )
            }

    terminal = acceptance.remember_with_review(
        Client(),
        mutation="real-public-writer",
        arguments={
            "title": "Hosted public writer relation review",
            "content": "## Observations\n- [acceptance] The public writer preserves exact reviewed draft state.\n",
            "note_type": "insight",
            "sources": [],
        },
        idempotency_key="real-public-writer-key",
    )

    assert terminal["state"] == "committed"
    assert calls[0][1] is None
    assert calls[0][0]["validate_only"] is True
    assert calls[1][1] == "real-public-writer-key"
    assert calls[1][0]["relation_disposition"] == "reviewed_none"
    assert calls[1][0]["relation_review_hash"] == calls[1][0]["draft_hash"]
    assert "relation_review_reason" in calls[1][0]


def test_protocol_failure_is_persisted_and_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="protocol-failure-001"
    )
    acceptance.prepare()
    acceptance.save_tokens({"access_token": "protocol-secret", "refresh_token": "refresh-secret"}, tenant="synthetic")
    acceptance.save_tokens({"access_token": "isolation-access", "refresh_token": "isolation-refresh"}, tenant="isolation")

    class Client:
        def initialize(self) -> dict[str, Any]:
            return {"protocolVersion": "2025-06-18"}

        def list_tools(self) -> dict[str, Any]:
            return {"tools": [{"name": "remember"}, {"name": "ask_memory"}, {"name": "read_memory"}]}

        def capture(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
            raise runner.AcceptanceError("receipt failure for protocol-secret")

    monkeypatch.setattr(runner.AcceptanceRunner, "_mcp_client", lambda *_args: Client())

    with pytest.raises(runner.AcceptanceError, match="protocol-secret"):
        acceptance.run_protocol()
    protocol = acceptance.manifest()["stages"]["protocol"]
    assert protocol["status"] == "failed"
    assert "protocol-secret" not in protocol["error"]
    assert "[REDACTED]" in protocol["error"]


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
                assert "id" not in request
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
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


def test_mcp_initialized_notification_is_idless_and_accepts_canonical_empty_202() -> None:
    runner = _load()
    notifications: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - HTTP handler API
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if request["method"] == "initialize":
                self._json({"jsonrpc": "2.0", "id": request["id"], "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "Hosted Exomem"}}})
                return
            if request["method"] == "notifications/initialized":
                notifications.append(request)
                if "id" in request:
                    self._json({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "Method not found"}})
                    return
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            assert self.headers["Mcp-Protocol-Version"] == "2025-06-18"
            self._json({"jsonrpc": "2.0", "id": request["id"], "result": {"tools": []}})

        def _json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    endpoint = f"http://127.0.0.1:{server.server_port}/api/exomem/mcp/v1"
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        client = runner.MCPClient(endpoint=endpoint, access_token="fixture-access", allow_loopback_fixture=True)

        client.initialize()
        assert client.list_tools() == {"tools": []}
        assert notifications == [{"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}]
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
                assert "id" not in request
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            else:
                tool = request["params"]["name"]
                if tool == "remember":
                    arguments = request["params"]["arguments"]
                    if arguments.get("validate_only") is True:
                        assert "Idempotency-Key" not in self.headers
                        result = {
                            "structuredContent": {
                                "state": "needs_review",
                                "diagnostics": {
                                    "draft_id": "cli-draft",
                                    "draft_hash": "a" * 64,
                                    "draft_token": "cli-token",
                                    "relation_review_hash": "a" * 64,
                                    "reviewed_none_required": True,
                                    "has_non_review_blockers": False,
                                    "committable_after_review": True,
                                },
                            }
                        }
                    else:
                        result = {"structuredContent": {"ok": True, "state": "committed", "terminal": True, "status": "committed", "mutated": True}}
                elif tool == "ask_memory":
                    tenant = self.headers["Authorization"].removeprefix("Bearer ").removesuffix("-access")
                    query = request["params"]["arguments"]["query"]
                    own_fact = runner.tenant_sentinel("cli-run-001", tenant)
                    assert query == "What tenant-local acceptance marker was recorded for run cli-run-001?"
                    result = {"structuredContent": {"result": {"hits": [{"path": f"Knowledge Base/Notes/Insights/{tenant}.md", "text": own_fact}]}}}
                else:
                    tenant = self.headers["Authorization"].removeprefix("Bearer ").removesuffix("-access")
                    result = {"structuredContent": {"result": {"content": runner.tenant_sentinel("cli-run-001", tenant)}}}
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
        assert [request["method"] for request in requests] == ["initialize", "notifications/initialized", "tools/list", "tools/call", "tools/call", "initialize", "notifications/initialized", "tools/call", "tools/call", "initialize", "notifications/initialized", "tools/list", "tools/call", "tools/call", "initialize", "notifications/initialized", "tools/call", "tools/call", "tools/call", "tools/call"]
        assert {request["headers"]["Authorization"] for request in requests} == {"Bearer synthetic-access", "Bearer isolation-access"}
    finally:
        server.shutdown()
        thread.join()


def test_protocol_refuses_a_genuine_foreign_tenant_sentinel_leak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="leak-001"
    )
    acceptance.prepare()
    for tenant in ("synthetic", "isolation"):
        acceptance.save_tokens({"access_token": f"{tenant}-access", "refresh_token": f"{tenant}-refresh"}, tenant=tenant)

    recall_counts = {"synthetic": 0, "isolation": 0}

    class Client:
        def __init__(self, tenant: str) -> None:
            self.tenant = tenant

        def initialize(self) -> dict[str, Any]:
            return {"protocolVersion": "2025-06-18"}

        def list_tools(self) -> dict[str, Any]:
            return {"tools": [{"name": "remember"}, {"name": "ask_memory"}, {"name": "read_memory"}]}

        def capture(self, arguments: dict[str, Any], **_kwargs: object) -> dict[str, Any]:
            if arguments.get("validate_only") is True:
                return {"structuredContent": {"state": "needs_review", "diagnostics": {"draft_id": "leak-draft", "draft_hash": "a" * 64, "draft_token": "leak-token", "relation_review_hash": "a" * 64, "reviewed_none_required": True, "has_non_review_blockers": False, "committable_after_review": True}}}
            return {"structuredContent": {"result": {"ok": True, "state": "committed", "terminal": True, "status": "committed", "mutated": True}}}

        def recall(self, query: str) -> dict[str, Any]:
            foreign = runner.tenant_sentinel("leak-001", "isolation")
            own = runner.tenant_sentinel("leak-001", self.tenant)
            assert query == "What tenant-local acceptance marker was recorded for run leak-001?"
            recall_counts[self.tenant] += 1
            fact = foreign if self.tenant == "synthetic" and recall_counts[self.tenant] == 2 else own
            return {"structuredContent": {"result": {"hits": [{"path": "Knowledge Base/Notes/Insights/sentinel.md", "text": fact}]}}}

        def call(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
            return {"structuredContent": {"result": {"content": runner.tenant_sentinel("leak-001", self.tenant)}}}

    monkeypatch.setattr(runner.AcceptanceRunner, "_mcp_client", lambda _self, tenant: Client(tenant))

    with pytest.raises(runner.AcceptanceError, match="cross-tenant"):
        acceptance.run_protocol()


def test_benchmark_uses_five_concurrent_tenant_clients_and_never_passes_incomplete_samples(tmp_path: Path) -> None:
    runner = _load()
    requests: list[dict[str, Any]] = []
    request_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with request_lock:
                requests.append({
                    "method": request["method"],
                    "authorization": self.headers["Authorization"],
                    "arguments": request.get("params", {}).get("arguments"),
                })
            if request["method"] == "initialize":
                result: dict[str, Any] = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "Hosted Exomem"}}
            elif request["method"] == "notifications/initialized":
                assert "id" not in request
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            elif request["method"] == "tools/list":
                result = {"tools": [{"name": "remember"}, {"name": "ask_memory"}, {"name": "read_memory"}]}
            else:
                tool = request["params"]["name"]
                if tool == "ask_memory":
                    tenant = self.headers["Authorization"].removeprefix("Bearer ").removesuffix("-access")
                    assert request["params"]["arguments"]["query"] == "What already-converged corpus marker belongs to run benchmark-001?"
                    fact = runner.tenant_sentinel("benchmark-001", tenant) + " corpus"
                    result = {"structuredContent": {"result": {"hits": [{"path": "Knowledge Base/Notes/Insights/corpus.md", "text": fact}]}}}
                elif tool == "read_memory":
                    tenant = self.headers["Authorization"].removeprefix("Bearer ").removesuffix("-access")
                    result = {"structuredContent": {"result": {"content": runner.tenant_sentinel("benchmark-001", tenant) + " corpus"}}}
                else:
                    arguments = request["params"]["arguments"]
                    if arguments.get("validate_only") is True:
                        result = {"structuredContent": {"state": "needs_review", "diagnostics": {"draft_id": "benchmark-draft", "draft_hash": "a" * 64, "draft_token": "benchmark-token", "relation_review_hash": "a" * 64, "reviewed_none_required": True, "has_non_review_blockers": False, "committable_after_review": True}}}
                    else:
                        result = {"structuredContent": {"ok": True, "state": "committed", "terminal": True, "status": "committed", "mutated": True}}
            body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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
        acceptance = runner.AcceptanceRunner.from_config(config_path, state_dir=tmp_path / "state", run_id="benchmark-001", allow_loopback_fixture=True)
        acceptance.prepare()
        corpus = runner.generate_synthetic_corpus(acceptance.run_dir / "fixtures" / "corpus")
        acceptance.register_fixture(acceptance.run_dir / "fixtures" / "corpus")
        manifest = acceptance.manifest()
        manifest["stages"]["performance"] = {"status": "pending", "fixture": corpus}
        acceptance._write_manifest(manifest)
        for tenant in ("synthetic", "isolation"):
            acceptance.save_tokens({"access_token": f"{tenant}-access", "refresh_token": f"{tenant}-refresh"}, tenant=tenant)

        acceptance.seed_corpus()
        assert runner.main(["benchmark", "--config", str(config_path), "--state-dir", str(tmp_path / "state"), "--run-id", "benchmark-001", "--resume"], allow_loopback_fixture=True) == 0

        evidence = acceptance.manifest()["stages"]["performance"]["evidence"]
        assert evidence["clients"] == 5
        assert evidence["warm_samples_per_operation"] == 100
        assert evidence["cold_client_resets"] == 20
        assert evidence["cold_reset_semantics"] == "new MCPClient instance; no service, process, tenant, or storage reset"
        assert evidence["capture_measurement_semantics"] == "timed public validate-only round trip, prepared-journal durable write, public commit round trip, and confirmed-journal durable write"
        assert evidence["warm_recall_semantics"] == "timed recall of the already-converged run-owned corpus fact; citation readback is verified outside the timed interval"
        assert {sample["authorization"] for sample in requests} == {"Bearer synthetic-access", "Bearer isolation-access"}
        assert len([sample for sample in requests if sample["method"] == "initialize"]) == 124
        benchmark_captures = [
            sample["arguments"]
            for sample in requests
            if isinstance(sample["arguments"], dict)
            and not sample["arguments"].get("validate_only")
            and str(sample["arguments"].get("title", "")).startswith("Hosted benchmark")
        ]
        assert len(benchmark_captures) == 100
        for capture in benchmark_captures:
            content = capture["content"]
            assert isinstance(content, str)
            parsed = semantic_units.parse_semantic_units(content)
            assert content.startswith("## Observations\n")
            assert parsed.errors == ()
            assert [(unit.form, unit.kind) for unit in parsed.units] == [("compact", "observation")]

        incomplete = runner.AcceptanceRunner.from_config(config_path, state_dir=tmp_path / "state", run_id="benchmark-002", allow_loopback_fixture=True)
        incomplete.prepare()
        for tenant in ("synthetic", "isolation"):
            incomplete.save_tokens({"access_token": f"{tenant}-access", "refresh_token": f"{tenant}-refresh"}, tenant=tenant)
        with pytest.raises(runner.AcceptanceError, match="corpus"):
            incomplete.run_benchmark()
        assert incomplete.manifest()["stages"]["performance"]["status"] == "pending"
    finally:
        server.shutdown()
        thread.join()


@pytest.mark.parametrize("failure", ("capture", "recall"))
def test_benchmark_refuses_failed_capture_and_unresolvable_warm_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id=f"benchmark-{failure}-001"
    )
    acceptance.prepare()
    corpus = runner.generate_synthetic_corpus(acceptance.run_dir / "fixtures" / "corpus")
    manifest = acceptance.manifest()
    facts = {
        tenant: runner.tenant_sentinel(acceptance.run_id, tenant) + " corpus"
        for tenant in ("synthetic", "isolation")
    }
    manifest["stages"]["performance"] = {
        "status": "pending",
        "fixture": corpus,
        "cell_corpus": {
            tenant: {"status": "committed", "convergence": "passed", "fact": fact}
            for tenant, fact in facts.items()
        },
    }
    acceptance._write_manifest(manifest)

    class Client:
        def __init__(self, tenant: str) -> None:
            self.tenant = tenant

        def initialize(self) -> dict[str, Any]:
            return {"protocolVersion": "2025-06-18"}

        def list_tools(self) -> dict[str, Any]:
            return {"tools": []}

        def capture(self, arguments: dict[str, Any], **_kwargs: object) -> dict[str, Any]:
            if arguments.get("validate_only") is True:
                return {"structuredContent": {"state": "needs_review", "diagnostics": {"draft_id": "failure-draft", "draft_hash": "a" * 64, "draft_token": "failure-token", "relation_review_hash": "a" * 64, "reviewed_none_required": True, "has_non_review_blockers": False, "committable_after_review": True}}}
            if failure == "capture":
                return {"isError": True, "structuredContent": {"result": {"message": "committed elsewhere"}}}
            return {"structuredContent": {"result": {"ok": True, "state": "committed", "terminal": True, "status": "committed", "mutated": True}}}

        def recall(self, _query: str) -> dict[str, Any]:
            hits = [] if failure == "recall" else [{"path": "Knowledge Base/Notes/Insights/corpus.md", "text": facts[self.tenant]}]
            return {"structuredContent": {"result": {"hits": hits}}}

        def call(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
            return {"structuredContent": {"result": {"content": facts[self.tenant]}}}

    monkeypatch.setattr(runner.AcceptanceRunner, "_mcp_client", lambda _self, tenant: Client(tenant))

    with pytest.raises(runner.AcceptanceError, match="warm samples"):
        acceptance.run_benchmark()
    assert acceptance.manifest()["stages"]["performance"]["status"] == "failed"


def test_benchmark_rerun_cannot_measure_cached_captures_as_fresh_latency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _load()
    acceptance = runner.AcceptanceRunner.from_config(
        _write_config(tmp_path), state_dir=tmp_path / "state", run_id="benchmark-replay-001"
    )
    acceptance.prepare()
    corpus = runner.generate_synthetic_corpus(acceptance.run_dir / "fixtures" / "corpus")
    facts = {
        tenant: runner.tenant_sentinel(acceptance.run_id, tenant) + " corpus"
        for tenant in ("synthetic", "isolation")
    }

    def stage() -> dict[str, Any]:
        return {
            "status": "pending",
            "fixture": corpus,
            "cell_corpus": {
                tenant: {"status": "committed", "convergence": "passed", "fact": fact}
                for tenant, fact in facts.items()
            },
        }

    manifest = acceptance.manifest()
    manifest["stages"]["performance"] = stage()
    acceptance._write_manifest(manifest)
    seen_keys: set[str] = set()
    local = threading.local()

    def clock() -> float:
        if getattr(local, "capture_started", False):
            local.capture_started = False
            return 1.1
        return 0.0

    class Client:
        def __init__(self, tenant: str) -> None:
            self.tenant = tenant

        def initialize(self) -> dict[str, Any]:
            return {"protocolVersion": "2025-06-18"}

        def list_tools(self) -> dict[str, Any]:
            return {"tools": []}

        def capture(
            self, arguments: dict[str, Any], *, idempotency_key: str | None = None
        ) -> dict[str, Any]:
            if arguments.get("validate_only") is True:
                assert idempotency_key is None
                return {"structuredContent": {"state": "needs_review", "diagnostics": {"draft_id": "replay-draft", "draft_hash": "a" * 64, "draft_token": "replay-token", "relation_review_hash": "a" * 64, "reviewed_none_required": True, "has_non_review_blockers": False, "committable_after_review": True}}}
            assert idempotency_key is not None
            if idempotency_key not in seen_keys:
                seen_keys.add(idempotency_key)
                local.capture_started = True
            return {"structuredContent": {"ok": True, "state": "committed", "terminal": True, "status": "committed", "mutated": True}}

        def recall(self, _query: str) -> dict[str, Any]:
            return {"structuredContent": {"result": {"hits": [{"path": "Knowledge Base/Notes/Insights/corpus.md", "text": facts[self.tenant]}]}}}

        def call(self, *_args: object, **_kwargs: object) -> dict[str, Any]:
            return {"structuredContent": {"result": {"content": facts[self.tenant]}}}

    monkeypatch.setattr(runner.time, "perf_counter", clock)
    monkeypatch.setattr(runner.AcceptanceRunner, "_mcp_client", lambda _self, tenant: Client(tenant))

    with pytest.raises(runner.AcceptanceError, match="capture"):
        acceptance.run_benchmark()
    manifest = acceptance.manifest()
    manifest["stages"]["performance"].update(stage())
    acceptance._write_manifest(manifest)
    with pytest.raises(runner.AcceptanceError, match="capture"):
        acceptance.run_benchmark()

    attempts = acceptance.manifest()["stages"]["performance"]["benchmark_attempts"]
    assert len(attempts) == 2
    assert attempts[0]["status"] == attempts[1]["status"] == "failed"
    assert attempts[0]["id"] != attempts[1]["id"]


def test_continuity_rotates_persisted_tokens_then_proves_service_use_after_fleet_window(tmp_path: Path) -> None:
    runner = _load()
    token_requests: list[dict[str, list[str]]] = []
    mcp_authorizations: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            assert self.path == "/.well-known/oauth-authorization-server/api/exomem/oauth"
            self._json({"issuer": base + "/api/exomem/oauth", "authorization_endpoint": base + "/api/exomem/oauth/authorize", "token_endpoint": base + "/api/exomem/oauth/token", "grant_types_supported": ["authorization_code", "refresh_token"], "code_challenge_methods_supported": ["S256"], "scopes_supported": ["exomem.read", "exomem.write", "offline_access"]})

        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/api/exomem/oauth/token":
                request = urllib.parse.parse_qs(body.decode())
                token_requests.append(request)
                assert request["grant_type"] == ["refresh_token"]
                assert request["client_id"] == ["hosted-acceptance"]
                assert request["resource"] == [base + "/api/exomem/mcp/v1"]
                if request["refresh_token"] == ["synthetic-refresh"]:
                    self._json({"access_token": "rotated-access", "refresh_token": "rotated-refresh", "expires_in": 900})
                else:
                    assert request["refresh_token"] == ["rotated-refresh"]
                    self._json({"access_token": "fleet-access", "refresh_token": "fleet-refresh", "expires_in": 900})
                return
            request = json.loads(body)
            mcp_authorizations.append(self.headers["Authorization"])
            assert self.headers["Authorization"] == "Bearer fleet-access"
            if request["method"] == "initialize":
                result: dict[str, Any] = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "Hosted Exomem"}}
            elif request["method"] == "notifications/initialized":
                assert "id" not in request
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            elif request["method"] == "tools/call" and request["params"]["name"] == "ask_memory":
                result = {"structuredContent": {"result": {"hits": [{"path": "Knowledge Base/Notes/Insights/acceptance.md", "text": runner.tenant_sentinel("continuity-001", "synthetic")}]}}}
            else:
                result = {"structuredContent": {"result": {"content": runner.tenant_sentinel("continuity-001", "synthetic")}}}
            self._json({"jsonrpc": "2.0", "id": request["id"], "result": result})

        def _json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
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
        first = runner.AcceptanceRunner.from_config(config_path, state_dir=tmp_path / "state", run_id="continuity-001", allow_loopback_fixture=True)
        first.prepare()
        first.save_tokens({"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "expires_at": 900.0}, tenant="synthetic")
        first.save_tokens({"access_token": "isolation-access", "refresh_token": "isolation-refresh"}, tenant="isolation")

        first.evaluate_continuity(now=0.0)
        first.evaluate_continuity(now=900.0)
        assert first.manifest()["stages"]["continuity"]["status"] == "pending"
        assert first.load_tokens(tenant="synthetic")["refresh_token"] == "rotated-refresh"
        assert token_requests

        resumed = runner.AcceptanceRunner.from_config(config_path, state_dir=tmp_path / "state", run_id="continuity-001", allow_loopback_fixture=True)
        resumed.prepare(resume=True)
        resumed.evaluate_continuity(now=3600.0)

        evidence = resumed.manifest()["stages"]["continuity"]["evidence"]
        assert evidence["refresh_rotation"] == "passed"
        assert evidence["post_fleet_service_use"] == "passed"
        assert evidence["citation"] == "Knowledge Base/Notes/Insights/acceptance.md"
        assert mcp_authorizations
    finally:
        server.shutdown()
        thread.join()
