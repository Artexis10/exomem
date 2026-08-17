from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from exomem import graph_sync, mutation_terminal

_SCRIPT = Path(__file__).parents[1] / "scripts" / "records_live_acceptance.py"
_SPEC = importlib.util.spec_from_file_location("records_live_acceptance", _SCRIPT)
assert _SPEC and _SPEC.loader
live = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(live)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_GRAPH_CHECKPOINT = graph_sync.GraphSyncCheckpoint.create(
    generation=1,
    mutation_id="0123456789abcdef01234567",
    paths=(),
    created_paths=(),
    scope="full",
)


@pytest.mark.parametrize(
    "build_outcome",
    [
        graph_sync.committed_graph_pending,
        graph_sync.committed_graph_queued,
        graph_sync.committed_graph_failure,
    ],
    ids=["pending_rebuild", "pending_queued", "failed"],
)
def test_acceptance_accepts_every_graph_outcome_the_server_can_emit(build_outcome) -> None:
    """This script is not in CI, so its graph vocabulary drifts silently.

    An outcome the server emits and this validator rejects fails only when an
    operator runs it against a real build -- which is exactly how a correct
    bounded write once looked like an acceptance regression. Driving the real
    emitters rather than restating their payloads here means a new outcome
    cannot be added without this failing first.
    """
    live._validate_compact_graph_outcome("revise", build_outcome(_GRAPH_CHECKPOINT))


def _hmac(value: str) -> str:
    return hmac.new(b"pairing-key", value.encode("utf-8"), hashlib.sha256).hexdigest()


_COLLECTION_ID = "11111111-1111-4111-8111-111111111111"
_ITEM_KEY = "22222222-2222-4222-8222-222222222222"


def _example() -> dict[str, Any]:
    return {
        "manifest_path": "Knowledge Base/Records/Example/_collection.md",
        "manifest_text": "---\nsemantic_profile: records\n---\n",
        "append_item": {"status": "created"},
    }


def _inspection(revision: int, *, gaps: list[str] | None = None) -> dict[str, Any]:
    return {
        "kind": "collection",
        "report_only": True,
        "contract": {"collection_id": _COLLECTION_ID},
        "snapshot": _digest(f"snapshot-{revision}"),
        "lifecycle_guards": {
            "expected_manifest_hash": _digest(f"manifest-{revision}"),
            "expected_container_hash": _digest(f"container-{revision}"),
        },
        "audit": {"status": "gap" if gaps else "ok", "gaps": gaps or []},
        "diagnostics": [],
    }


def _terminal(action: str, revision: int) -> dict[str, Any]:
    before = _digest(f"before-{action}-{revision}")
    after = _digest(f"after-{action}-{revision}")
    if action in {"revise", "rebaseline"}:
        receipt: dict[str, Any] = {
            "_record_receipt": "exomem.records-mutation",
            "receipt_version": 2,
            "operation": action,
            "collection_id": _COLLECTION_ID,
            "item_key": None,
            "before_item_hash": None,
            "after_item_hash": None,
            "before_manifest_hash": before,
            "after_manifest_hash": after,
            "before_container_hash": before,
            "after_container_hash": after,
            "affected_paths": ["Knowledge Base/Records/Example/_collection.md"],
            "payload_hash": _digest(f"payload-{action}-{revision}"),
            "outcome": "committed",
            "audit_correlation": "a" * 24,
            "continuity": action == "revise",
            "acknowledged_gap_codes": [] if action == "revise" else ["current-container-mismatch"],
            "gap_fingerprint": None if action == "revise" else _digest("gap"),
            "checkpoint_snapshot_hash": None if action == "revise" else _digest("checkpoint"),
            "minimum_reader_version": 2,
        }
    else:
        receipt = {
            "_record_receipt": "exomem.records-mutation",
            "receipt_version": 1,
            "operation": action,
            "collection_id": _COLLECTION_ID,
            "item_key": None if action == "create" else _ITEM_KEY,
            "before_item_hash": None if action in {"create", "append"} else before,
            "after_item_hash": after,
            "before_container_hash": None if action == "create" else before,
            "after_container_hash": after,
            "affected_paths": ["Knowledge Base/Records/Example/_collection.md"],
            "payload_hash": None if action in {"create", "update"} else _digest(f"payload-{action}-{revision}"),
            "outcome": "committed",
            "audit_correlation": "a" * 24,
        }
    assert mutation_terminal.valid_record_receipt(receipt)
    return mutation_terminal.project_terminal(
        mutation_terminal.committed_terminal(
            receipt,
            request_id=f"request-{action}-{revision}",
            receipt_id=f"receipt-{action}-{revision}",
            idempotency_key=f"idempotency-{action}-{revision}",
        )
    )


def _evidence() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "deployment": {"sha256": _digest("deployment")},
        "release": {"package": "exomem", "version": "9.9.9"},
        "surface": {"mcp_digest": _digest("surface")},
        "run": {
            "nonce": "run-20260812-a1b2c3d4",
            "timestamp": "2026-08-12T10:00:00Z",
            "expires_at": "2026-08-12T11:00:00Z",
        },
        "vault": {"purpose": "records-live-acceptance", "reset_epoch": "reset-42"},
        "identity": {
            "principal_hmac_sha256": _hmac("principal"),
            "audience_hmac_sha256": _hmac("audience"),
        },
        "client_contracts": {
            "transport": {
                "client": "transport",
                "client_version": "1.0.0",
                "model_version": "deterministic",
                "system_contract_version": "records-v2",
            }
        },
        "actions": [
            {"action": "describe", "outcome": "completed"},
            {"action": "validate", "outcome": "completed"},
            {"action": "create", "outcome": "committed"},
        ],
        "mutations": [
            {
                "action": "create",
                "request_id": "request-create-1",
                "receipt_id": "receipt-create-1",
                "terminal_outcome": "committed",
                "before_readback_sha256": _digest("before"),
                "after_readback_sha256": _digest("after"),
            }
        ],
        "restart": {"outcome": "completed", "readback_sha256": _digest("restart")},
        "prompt_cases": [
            {"id": "existing-collection", "sha256": _digest("case-a")},
            {"id": "no-collection", "sha256": _digest("case-b")},
        ],
        "graph_availability": {"proof_digest": _digest("graph")},
    }


def _expected() -> live.RecordsEvidenceExpectation:
    return live.RecordsEvidenceExpectation(
        deployment_sha256=_digest("deployment"),
        package="exomem",
        release_version="9.9.9",
        surface_digest=_digest("surface"),
        vault_purpose="records-live-acceptance",
        reset_epoch="reset-42",
        principal_hmac_sha256=_hmac("principal"),
        audience_hmac_sha256=_hmac("audience"),
        client_contracts={
            "transport": ("transport", "1.0.0", "deterministic", "records-v2")
        },
        required_actions=frozenset({"describe", "validate", "create"}),
        required_prompt_cases={
            "existing-collection": _digest("case-a"),
            "no-collection": _digest("case-b"),
        },
        graph_proof_digest=_digest("graph"),
    )


def test_closed_evidence_accepts_complete_content_free_facts() -> None:
    rendered = live.validate_records_live_evidence(
        _evidence(), expected=_expected(), now="2026-08-12T10:30:00Z"
    )

    assert rendered == live.canonical_json(_evidence())
    assert "secret-value" not in rendered.decode("utf-8")


def test_evidence_requires_exact_contracts_for_every_approved_client() -> None:
    evidence = _evidence()
    evidence.pop("client_contracts")
    evidence["client_contracts"] = {
        "codex": {
            "client": "codex",
            "client_version": "1.0.0",
            "model_version": "deterministic",
            "system_contract_version": "records-v2",
        },
        "claude-code": {
            "client": "claude-code",
            "client_version": "1.0.0",
            "model_version": "deterministic",
            "system_contract_version": "records-v2",
        },
    }
    expected = _expected()
    expected.client_contracts = {
        "codex": ("codex", "1.0.0", "deterministic", "records-v2"),
        "claude-code": ("claude-code", "1.0.0", "deterministic", "records-v2"),
    }

    assert live.validate_records_live_evidence(
        evidence, expected=expected, now="2026-08-12T10:30:00Z"
    ) == live.canonical_json(evidence)
    evidence["client_contracts"]["codex"]["client"] = "claude-code"
    with pytest.raises(live.RecordsEvidenceError, match="client contracts"):
        live.validate_records_live_evidence(
            evidence, expected=expected, now="2026-08-12T10:30:00Z"
        )


def test_expired_evidence_can_be_verified_for_durable_distribution_only() -> None:
    evidence = _evidence()
    evidence["run"]["expires_at"] = "2026-08-12T10:01:00Z"

    assert live.validate_records_live_evidence(
        evidence,
        expected=_expected(),
        now="2026-08-13T10:30:00Z",
        require_fresh=False,
    ) == live.canonical_json(evidence)


def test_lifecycle_evidence_binds_v2_profile_and_reader_floor() -> None:
    evidence = _evidence()
    evidence["release"] = {
        "package": "exomem",
        "version": "9.9.9",
        "profile": "hosted-alpha-agent-v2",
        "minimum_records_reader_version": 2,
    }

    expected = _expected()
    expected.profile = "hosted-alpha-agent-v2"
    expected.minimum_records_reader_version = 2

    rendered = live.validate_records_live_evidence(
        evidence, expected=expected, now="2026-08-12T10:30:00Z"
    )

    assert rendered == live.canonical_json(evidence)


def test_evidence_requires_committed_mutations_and_completed_reads() -> None:
    evidence = _evidence()
    evidence["actions"][-1]["outcome"] = "completed"
    evidence["mutations"] = []

    with pytest.raises(live.RecordsEvidenceError, match="exact action terminals"):
        live.validate_records_live_evidence(
            evidence, expected=_expected(), now="2026-08-12T10:30:00Z"
        )


def test_http_transport_uses_mcp_json_rpc_tools_call(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _Response:
        headers = {"Content-Type": "application/json"}

        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return self._body

    def fake_urlopen(request: Any, *, timeout: float) -> _Response:
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": captured["payload"]["id"],
                    "result": {"structuredContent": {"outcome": "completed"}},
                }
            ).encode("utf-8")
        )

    monkeypatch.setattr(live, "urlopen", fake_urlopen)
    transport = live.HttpOAuthRecordsTransport(
        "https://records.example.test",
        "token",
        reset_path="/control/reset",
        restart_path="/control/restart",
    )

    assert transport.invoke(action="describe", timeout_seconds=2.0) == {"outcome": "completed"}
    assert captured["url"] == "https://records.example.test/mcp"
    assert captured["headers"]["Accept"] == "application/json, text/event-stream"
    assert captured["payload"]["method"] == "tools/call"
    assert captured["payload"]["params"] == {
        "name": "record_memory", "arguments": {"action": "describe"}
    }


def test_http_transport_decodes_a_framed_fastmcp_sse_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        headers = {"Content-Type": "text/event-stream; charset=utf-8"}

        def __init__(self, body: bytes) -> None:
            self._body = body

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return self._body

    def fake_urlopen(request: Any, *, timeout: float) -> _Response:
        request_payload = json.loads(request.data)
        envelope = {
            "jsonrpc": "2.0",
            "id": request_payload["id"],
            "result": {"structuredContent": {"outcome": "completed"}},
        }
        return _Response(f"data: {json.dumps(envelope)}\n\n".encode("utf-8"))

    monkeypatch.setattr(live, "urlopen", fake_urlopen)
    transport = live.HttpOAuthRecordsTransport("https://records.example.test", "token")

    assert transport.invoke(action="describe", timeout_seconds=2.0) == {"outcome": "completed"}


def test_http_transport_extracts_actual_fastmcp_text_content_when_structured_content_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": self.request_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps({"contract_version": 1, "examples": {}}),
                            }
                        ],
                        "isError": False,
                    },
                }
            ).encode("utf-8")

    response = _Response()

    def fake_urlopen(request: Any, *, timeout: float) -> _Response:
        response.request_id = json.loads(request.data)["id"]
        return response

    monkeypatch.setattr(live, "urlopen", fake_urlopen)

    assert live.HttpOAuthRecordsTransport("https://records.example.test", "token").invoke(
        action="describe", timeout_seconds=2.0
    ) == {"contract_version": 1, "examples": {}}


@pytest.mark.parametrize(
    ("kind", "match"),
    [
        ("malformed", "malformed framing"),
        ("multiple", "exactly one JSON-RPC"),
        ("error", "contains an error"),
        ("truncated", "truncated"),
        ("oversize", "size limit"),
        ("non-json", "not JSON"),
        ("wrong-id", "does not match request id"),
        ("too-many-events", "event limit"),
    ],
)
def test_http_transport_refuses_invalid_fastmcp_sse_responses(
    kind: str, match: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Response:
        def __init__(self, body: bytes, content_type: str = "text/event-stream") -> None:
            self._body = body
            self.headers = {"Content-Type": content_type}

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return self._body

    def fake_urlopen(request: Any, *, timeout: float) -> _Response:
        request_id = json.loads(request.data)["id"]
        envelope = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"structuredContent": {"outcome": "completed"}},
        }
        event = f"data: {json.dumps(envelope)}\n\n".encode("utf-8")
        if kind == "malformed":
            return _Response(event.replace(b"data:", b"data", 1))
        if kind == "multiple":
            conflicting = dict(envelope)
            conflicting["result"] = {"structuredContent": {"outcome": "different"}}
            return _Response(event + f"data: {json.dumps(conflicting)}\n\n".encode())
        if kind == "error":
            return _Response(
                f"data: {json.dumps({'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -1}})}\n\n".encode()
            )
        if kind == "truncated":
            return _Response(event[:-1])
        if kind == "oversize":
            return _Response(b"x" * (live._MAX_HTTP_RESPONSE_BYTES + 1), "application/json")
        if kind == "non-json":
            return _Response(b"data: not-json\n\n")
        if kind == "wrong-id":
            envelope["id"] = "other-request"
            return _Response(f"data: {json.dumps(envelope)}\n\n".encode())
        return _Response(event * (live._MAX_SSE_EVENTS + 1))

    monkeypatch.setattr(live, "urlopen", fake_urlopen)
    transport = live.HttpOAuthRecordsTransport("https://records.example.test", "token")

    with pytest.raises(live.RecordsEvidenceError, match=match):
        transport.invoke(action="describe", timeout_seconds=2.0)


def test_cli_renders_closed_facts_from_a_real_protocol_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    actions_path = tmp_path / "actions.json"
    cases_path = tmp_path / "cases.json"
    contracts_path = tmp_path / "contracts.json"
    actions_path.write_text(
        json.dumps(
            {
                "create": {"why": "create"},
                "append": {"item_key": "11111111-1111-4111-8111-111111111111", "why": "append"},
                "update": {"changes": {"status": "updated"}, "why": "update"},
                "revise": {"why": "revise"},
                "rebaseline": {"why": "rebaseline"},
            }
        ),
        encoding="utf-8",
    )
    cases_path.write_text(
        json.dumps(
            {
                "codex-existing-collection": {
                    "sha256": _digest("case-a"),
                    "client": "codex",
                    "action": "append",
                    "outcome": "committed",
                    "mutation": True,
                }
            }
        ),
        encoding="utf-8",
    )
    contracts_path.write_text(
        json.dumps(
            {
                "codex": {
                    "client": "codex",
                    "client_version": "1.0.0",
                    "model_version": "deterministic",
                    "system_contract_version": "records-v2",
                },
                "claude-code": {
                    "client": "claude-code",
                    "client_version": "1.0.0",
                    "model_version": "deterministic",
                    "system_contract_version": "records-v2",
                },
            }
        ),
        encoding="utf-8",
    )

    class _Response:
        def __init__(self, body: bytes, content_type: str) -> None:
            self._body = body
            self.headers = {"Content-Type": content_type}

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return self._body

    reads = 0
    requests: list[dict[str, Any]] = []
    terminals: dict[str, dict[str, Any]] = {}

    def fake_urlopen(request: Any, *, timeout: float) -> _Response:
        nonlocal reads
        if request.full_url.endswith("/control/reset"):
            return _Response(b'{"reset_epoch":"reset-42"}', "application/json")
        if request.full_url.endswith("/control/restart"):
            return _Response(b'{"outcome":"completed"}', "application/json")
        if request.full_url.endswith("/control/direct-edit"):
            return _Response(b'{"status":"committed","mutated":true}', "application/json")
        if request.full_url.endswith("/control/precreate"):
            return _Response(
                json.dumps(
                    {
                        "manifest_path": _example()["manifest_path"],
                        "absent": True,
                        "state_sha256": _digest("absent"),
                    }
                ).encode(),
                "application/json",
            )
        payload = json.loads(request.data)
        requests.append(payload["params"]["arguments"])
        action = payload["params"]["arguments"]["action"]
        if action == "inspect":
            reads += 1
            structured: dict[str, Any] = _inspection(
                reads, gaps=["current-container-mismatch"] if reads > 8 else None
            )
        elif action == "describe":
            structured = {
                "contract_version": 1,
                "records_action_constraint": {"semantic_profile": "records"},
                "examples": {"minimal": _example()},
            }
        elif action == "validate":
            structured = {"valid": True, "normalized_contract": {"semantic_profile": "records"}}
        elif action == "query":
            structured = {
                "collection_id": _COLLECTION_ID,
                "snapshot": _digest(f"query-{reads}"),
                "rows": [],
                "returned": 0,
                "total_matched": 0,
                "truncated": False,
                "derived": True,
            }
        else:
            structured = _terminal(action, reads)
            terminals[action] = structured
        envelope = {
            "jsonrpc": "2.0",
            "id": payload["id"],
            "result": {"structuredContent": structured},
        }
        return _Response(f"event: message\ndata: {json.dumps(envelope)}\n\n".encode(), "text/event-stream")

    monkeypatch.setattr(live, "urlopen", fake_urlopen)
    digests = {
        "deployment": _digest("deployment"),
        "surface": _digest("surface"),
        "principal": _hmac("principal"),
        "audience": _hmac("audience"),
        "graph": _digest("graph"),
    }
    argv = [
        "--endpoint", "https://records.example.test", "--bearer-token", "token",
        "--purpose", "records-live-acceptance", "--reset-path", "/control/reset",
        "--restart-path", "/control/restart", "--direct-edit-path", "/control/direct-edit",
        "--precreate-path", "/control/precreate", "--action-requests", str(actions_path),
        "--prompt-cases", str(cases_path), "--deployment-sha256", digests["deployment"],
        "--release-version", "9.9.9", "--profile", "hosted-alpha-agent-v2",
        "--minimum-records-reader-version", "2", "--surface-digest", digests["surface"],
        "--reset-epoch", "reset-42", "--principal-hmac-sha256", digests["principal"],
        "--audience-hmac-sha256", digests["audience"], "--client-contracts", str(contracts_path),
        "--graph-proof-digest", digests["graph"],
    ]
    for action in ("describe", "validate", "create", "inspect", "query", "append", "update", "revise", "rebaseline"):
        argv.extend(("--action", action))

    assert live.main(argv) == 0
    facts = json.loads(capsys.readouterr().out)
    assert set(facts) == set(live._TOP_LEVEL_FIELDS)
    assert facts["run"]["expires_at"] > facts["run"]["timestamp"]
    update = next(item for item in requests if item["action"] == "update")
    rebaseline = next(item for item in requests if item["action"] == "rebaseline")
    assert update["expected_item_version"] == terminals["append"]["after_item_hash"]
    assert rebaseline["acknowledged_gap_codes"] == ["current-container-mismatch"]


@pytest.mark.parametrize(
    ("action", "response", "match"),
    [
        ("describe", {"ok": False}, "unsuccessful"),
        ("describe", {"error": {"code": "DENIED"}}, "unsuccessful"),
        ("inspect", {"withheld": True}, "unsuccessful"),
        ("validate", {"valid": False, "normalized_contract": {}}, "unsuccessful"),
        ("create", {"valid": False}, "unsuccessful"),
        ("query", {"status": "failed"}, "unsuccessful"),
        ("inspect", {"isError": True}, "unsuccessful"),
        ("describe", {"contract_version": 1}, "Records contract"),
        ("validate", {"valid": True}, "valid Records contract"),
        ("inspect", {"kind": "collection", "report_only": True}, "Records state"),
        ("query", {"rows": [], "derived": True, "returned": 0, "total_matched": 0, "truncated": False}, "projected Records rows"),
        ("create", {"status": "replayed", "mutated": True}, "unsuccessful"),
        ("create", {"outcome": "completed"}, "compact Records mutation"),
    ],
)
def test_runner_refuses_unsuccessful_or_incomplete_record_memory_terminals(
    action: str, response: dict[str, Any], match: str
) -> None:
    with pytest.raises(live.RecordsEvidenceError, match=match):
        live._validated_action_terminal(action, response)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda response: response.pop("request_id"),
        lambda response: response.__setitem__("unexpected", "value"),
        lambda response: response.__setitem__("operation", "update"),
        lambda response: response.__setitem__("after_item_hash", "not-a-digest"),
        lambda response: response.__setitem__("audit_correlation", "not-a-correlation"),
    ],
)
def test_runner_refuses_incomplete_or_tampered_compact_records_receipts(mutate: Any) -> None:
    response = _terminal("append", 1)
    mutate(response)

    with pytest.raises(live.RecordsEvidenceError):
        live._validated_action_terminal("append", response)


@pytest.mark.parametrize(
    "indicator",
    [
        {"status": "committed"},
        {"status": "replayed"},
        {"status": "failed"},
        {"mutated": False},
        {"operation": "append"},
        {"request_id": "request-1"},
        {"receipt_id": "receipt-1"},
        {"outcome": "committed"},
    ],
)
def test_runner_refuses_mutation_indicators_on_a_read_response(indicator: dict[str, Any]) -> None:
    response = {
        "contract_version": 1,
        "records_action_constraint": {"semantic_profile": "records"},
        "examples": {"minimal": _example()},
    }
    response.update(indicator)

    with pytest.raises(live.RecordsEvidenceError, match="unsuccessful|mutation terminal"):
        live._validated_action_terminal("describe", response)


def test_full_runner_refuses_replayed_direct_edit_control() -> None:
    class _Transport:
        revision = 0
        drifted = False

        def reset(self, *, purpose: str, timeout_seconds: float) -> dict[str, str]:
            return {"reset_epoch": "reset-42"}

        def precreate(self, *, manifest_path: str, timeout_seconds: float) -> dict[str, Any]:
            return {"manifest_path": manifest_path, "absent": True, "state_sha256": _digest("absent")}

        def invoke(
            self, *, action: str, timeout_seconds: float, arguments: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            if action == "describe":
                return {
                    "contract_version": 1,
                    "records_action_constraint": {"semantic_profile": "records"},
                    "examples": {"minimal": _example()},
                }
            if action == "validate":
                return {"valid": True, "normalized_contract": {"semantic_profile": "records"}}
            if action == "inspect":
                return _inspection(
                    self.revision, gaps=["current-container-mismatch"] if self.drifted else None
                )
            if action == "query":
                return {
                    "collection_id": _COLLECTION_ID,
                    "snapshot": _digest(f"query-{self.revision}"),
                    "rows": [],
                    "returned": 0,
                    "total_matched": 0,
                    "truncated": False,
                    "derived": True,
                }
            return _terminal(action, self.revision)

        def readback(self, *, collection: str | None = None, timeout_seconds: float) -> dict[str, Any]:
            self.revision += 1
            return _inspection(
                self.revision, gaps=["current-container-mismatch"] if self.drifted else None
            )

        def direct_edit(self, *, collection: str, timeout_seconds: float) -> dict[str, Any]:
            self.drifted = True
            return {"status": "replayed", "mutated": True}

        def restart(self, *, timeout_seconds: float) -> dict[str, str]:
            return {"outcome": "completed"}

    runner = live.RecordsLiveAcceptanceRunner(
        _Transport(),
        timeout_seconds=2.0,
        expected_actions=(
            "describe", "validate", "create", "inspect", "query", "append", "update", "revise", "rebaseline"
        ),
        reset_purpose="records-live-acceptance",
    )

    with pytest.raises(live.RecordsEvidenceError, match="direct-edit control"):
        runner.run()


def test_full_runner_executes_real_record_memory_lifecycle_on_a_temp_vault(tmp_path: Path) -> None:
    from exomem import commands
    from exomem.writer_lease import LeaseConfig, LeaseManager

    class _DomainTransport:
        def __init__(self) -> None:
            self.root = tmp_path
            (self.root / "Knowledge Base/log.md").parent.mkdir(parents=True)
            (self.root / "Knowledge Base/log.md").write_text("# Activity\n", encoding="utf-8")
            self.item_path: str | None = None
            self.request_number = 0
            self.compact_actions: list[str] = []
            self.command = next(command for command in commands.PRODUCT_COMMANDS if command.name == "record_memory")
            self.manager = LeaseManager(
                LeaseConfig.from_env({"EXOMEM_WRITER_LEASE_STATE_DIR": str(self.root / "state")})
            )

        def reset(self, *, purpose: str, timeout_seconds: float) -> dict[str, str]:
            return {"reset_epoch": "reset-42"}

        def precreate(self, *, manifest_path: str, timeout_seconds: float) -> dict[str, Any]:
            assert not (self.root / manifest_path).exists()
            return {"manifest_path": manifest_path, "absent": True, "state_sha256": _digest("pre-create")}

        def invoke(
            self, *, action: str, timeout_seconds: float, arguments: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            values = dict(arguments or {})
            values["action"] = action
            if action in {"create", "append", "update", "revise", "rebaseline"}:
                values.setdefault("why", f"live {action}")
            if action == "update":
                values.setdefault("changes", {"label": "Updated event"})
            is_read = commands.invocation_is_read_only(self.command, values)
            if not is_read:
                self.request_number += 1
                values["response_detail"] = "compact"
            result = self.manager.invoke(
                self.command,
                (self.root,),
                values,
                read_only=is_read,
                idempotency_key=None if is_read else f"records-live-{self.request_number}",
                public_idempotency_key=None if is_read else f"records-live-{self.request_number}",
            )
            if not is_read:
                assert "leaf_result" not in result
                assert result["status"] == "committed"
                assert result["mutated"] is True
                self.compact_actions.append(action)
            if action == "append":
                self.item_path = result["affected_paths"][0]
            return result

        def readback(self, *, collection: str | None = None, timeout_seconds: float) -> dict[str, Any]:
            assert collection is not None
            return self.manager.invoke(
                self.command,
                (self.root,),
                {"action": "inspect", "collection": collection},
                read_only=True,
            )

        def direct_edit(self, *, collection: str, timeout_seconds: float) -> dict[str, Any]:
            assert self.item_path is not None
            item = self.root / self.item_path
            item.write_text(item.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            return {"status": "committed", "mutated": True}

        def restart(self, *, timeout_seconds: float) -> dict[str, str]:
            return {"outcome": "completed"}

    transport = _DomainTransport()
    facts = live.RecordsLiveAcceptanceRunner(
        transport,
        timeout_seconds=2.0,
        expected_actions=(
            "describe", "validate", "create", "inspect", "query", "append", "update", "revise", "rebaseline"
        ),
        reset_purpose="records-live-acceptance",
        expected_reset_epoch="reset-42",
    ).run()

    assert transport.compact_actions == ["create", "append", "update", "revise", "rebaseline"]

    evidence = _evidence()
    evidence.update(facts)
    expected = _expected()
    expected.required_actions = frozenset(
        {"describe", "validate", "create", "inspect", "query", "append", "update", "revise", "rebaseline"}
    )
    live.validate_records_live_evidence(evidence, expected=expected, now="2026-08-12T10:30:00Z")


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda item: item.pop("run"), "missing fields"),
        (lambda item: item.__setitem__("prose", "trust me"), "unknown fields"),
        (lambda item: item["run"].__setitem__("expires_at", "2026-08-12T09:00:00Z"), "expired"),
        (lambda item: item["vault"].__setitem__("reset_epoch", "reset-old"), "reset"),
        (lambda item: item["client_contracts"]["transport"].__setitem__("model_version", "other"), "client contract"),
        (lambda item: item["surface"].__setitem__("mcp_digest", _digest("old")), "surface"),
        (lambda item: item["mutations"][0].__setitem__("after_readback_sha256", _digest("before")), "readback"),
        (lambda item: item["actions"].pop(), "required actions"),
        (lambda item: item["prompt_cases"].pop(), "prompt cases"),
    ],
)
def test_closed_evidence_refuses_invalid_or_incomplete_facts(mutate: Any, match: str) -> None:
    evidence = json.loads(json.dumps(_evidence()))
    mutate(evidence)

    with pytest.raises(live.RecordsEvidenceError, match=match):
        live.validate_records_live_evidence(
            evidence, expected=_expected(), now="2026-08-12T10:30:00Z"
        )


def test_acceptance_ledger_replays_exact_evidence_only() -> None:
    facts = _evidence()
    envelope = {
        "facts": facts,
        "operator_signature": hmac.new(
            b"operator-secret", live.canonical_json(facts), hashlib.sha256
        ).hexdigest(),
    }
    ledger = live.RecordsAcceptanceLedger()

    first = ledger.accept(
        candidate_digest=_digest("candidate"),
        envelope=envelope,
        expected=_expected(),
        operator_secret="operator-secret",
        now="2026-08-12T10:30:00Z",
    )
    replay = ledger.accept(
        candidate_digest=_digest("candidate"),
        envelope=envelope,
        expected=_expected(),
        operator_secret="operator-secret",
        now="2026-08-12T10:30:00Z",
    )

    assert first == replay
    assert first["accepted"] is True
    with pytest.raises(live.RecordsEvidenceError, match="different candidate"):
        ledger.accept(
            candidate_digest=_digest("candidate-2"),
            envelope=envelope,
            expected=_expected(),
            operator_secret="operator-secret",
            now="2026-08-12T10:30:00Z",
        )


def test_operator_signed_envelope_requires_operator_held_signature() -> None:
    facts = _evidence()
    envelope = {
        "facts": facts,
        "operator_signature": hmac.new(
            b"operator-secret", live.canonical_json(facts), hashlib.sha256
        ).hexdigest(),
    }

    assert live.validate_operator_signed_records_evidence(
        envelope,
        expected=_expected(),
        operator_secret="operator-secret",
        now="2026-08-12T10:30:00Z",
    ) == live.canonical_json(envelope)
    with pytest.raises(live.RecordsEvidenceError, match="signature"):
        live.validate_operator_signed_records_evidence(
            {"facts": facts},
            expected=_expected(),
            operator_secret="operator-secret",
            now="2026-08-12T10:30:00Z",
        )


def test_runner_resets_then_records_only_content_free_transport_facts() -> None:
    transport = _Transport()
    runner = live.RecordsLiveAcceptanceRunner(
        transport,
        timeout_seconds=2.0,
        expected_actions=("describe", "create"),
        reset_purpose="records-live-acceptance",
    )

    facts = runner.run()

    assert transport.calls == ["reset", "describe", "readback", "create", "readback", "restart", "readback"]
    assert facts["vault"] == {"purpose": "records-live-acceptance", "reset_epoch": "reset-42"}
    assert facts["mutations"][0]["request_id"] == "request-create-1"
    assert "secret-value" not in live.canonical_json(facts).decode("utf-8")


def test_runner_derives_terminals_from_actual_record_memory_domain_results() -> None:
    class _ActualTransport:
        reads = 0

        def reset(self, *, purpose: str, timeout_seconds: float) -> dict[str, str]:
            return {"reset_epoch": "reset-42"}

        def invoke(self, *, action: str, timeout_seconds: float) -> dict[str, Any]:
            if action == "describe":
                return {
                    "contract_version": 1,
                    "records_action_constraint": {"semantic_profile": "records"},
                    "examples": {"minimal": _example()},
                }
            return _terminal(action, 1)

        def readback(self, *, timeout_seconds: float) -> dict[str, Any]:
            self.reads += 1
            return _inspection(self.reads)

        def restart(self, *, timeout_seconds: float) -> dict[str, str]:
            return {"status": "committed", "mutated": False}

    facts = live.RecordsLiveAcceptanceRunner(
        _ActualTransport(),
        timeout_seconds=2.0,
        expected_actions=("describe", "create"),
        reset_purpose="records-live-acceptance",
    ).run()

    assert facts["actions"] == [
        {"action": "describe", "outcome": "completed"},
        {"action": "create", "outcome": "committed"},
    ]


class _Transport:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def reset(self, *, purpose: str, timeout_seconds: float) -> dict[str, str]:
        assert purpose == "records-live-acceptance"
        assert timeout_seconds == 2.0
        self.calls.append("reset")
        return {"reset_epoch": "reset-42"}

    def invoke(self, *, action: str, timeout_seconds: float) -> dict[str, Any]:
        assert timeout_seconds == 2.0
        self.calls.append(action)
        if action == "describe":
            return {
                "contract_version": 1,
                "records_action_constraint": {"semantic_profile": "records"},
                "examples": {"minimal": _example()},
            }
        return _terminal(action, 1)

    def readback(self, *, timeout_seconds: float) -> dict[str, Any]:
        assert timeout_seconds == 2.0
        self.calls.append("readback")
        return _inspection(len(self.calls))

    def restart(self, *, timeout_seconds: float) -> dict[str, Any]:
        assert timeout_seconds == 2.0
        self.calls.append("restart")
        return {"status": "committed", "mutated": False}
