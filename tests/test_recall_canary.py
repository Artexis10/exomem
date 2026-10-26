"""Read-only public MCP case proof and content-free availability reports."""

from __future__ import annotations

import asyncio
import json
from typing import get_type_hints

import pytest

from exomem import recall_canary as gate
from exomem.request_latency_contract import load_contract


def _canary_case(**expected):
    return {
        "id": "case-01",
        "tool": "ask_memory",
        "arguments": {"query": "private-question"},
        "expected": {
            "state_path": ["ok"],
            "state": True,
            "outcome": "ok",
            "references": [{"path": ["hits", 0, "ref"], "value": "private-reference"}],
            "material_paths": [["hits", 0, "text"]],
            **expected,
        },
    }


def _run_canary(tmp_path, server, cases, **kwargs):
    from fastmcp import Client

    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    options = {
        "samples": 30,
        "warm_up": True,
        "contention": "uncontended",
        "deployment_id": "verified-deployment",
        "deployment_verified": True,
        "package_mode": "source",
        **kwargs,
    }
    return asyncio.run(
        gate.run_mcp_canary(
            client=Client(server),
            cases_file=path,
            **options,
        )
    )


def test_mcp_canary_proves_material_over_parsed_public_calls(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    report = _run_canary(tmp_path, server, [_canary_case()])
    assert report["verdict"] == "passed"
    assert report["availability"] == "passed"
    case = report["cases"][0]
    assert case["measured_attempts"] == 30
    assert case["warmth"] == "successful-case-proof"
    assert case["latency"][0]["boundary"] == "mcp-roundtrip-v1"
    assert case["latency"][0]["samples"] == 30
    assert case["latency"][0]["outcome_inclusion"] == ["ok", "error", "refused", "abstained"]
    assert report["initialization_ms"] >= 0
    assert all(item["elapsed_ms"] >= 0 for item in case["attempts"])
    assert report["observer"]["role"] == "observer-only"
    rendered = json.dumps(report)
    for secret in ("private-question", "private-reference", "verified-deployment"):
        assert secret not in rendered


def test_mcp_canary_accepts_a_positive_read_memory_case(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def read_memory(path: str) -> dict:
        return {"ok": True, "path": path, "body": "page material"}

    case = _canary_case(
        state_path=["ok"],
        state=True,
        references=[{"path": ["path"], "value": "private-page"}],
        material_paths=[["body"]],
    )
    case.update(tool="read_memory", arguments={"path": "private-page"})

    report = _run_canary(tmp_path, server, [case])

    assert report["availability"] == "passed"
    assert report["positive_coverage"]["read_memory"]["successful_cases"] == 1
    assert report["verdict"] == "passed"
    assert "private-page" not in json.dumps(report)


@pytest.mark.parametrize("tool", ["search_memory", "recall_context"])
def test_mcp_canary_calls_canonical_recall_tools(tmp_path, tool):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool(name=tool)
    def recall(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    case = _canary_case()
    case["tool"] = tool
    report = _run_canary(tmp_path, server, [case], samples=1, check="availability")
    assert report["check_verdict"] == "passed"
    assert report["cases"][0]["tool"] == tool


@pytest.mark.parametrize("tool", ["read_records", "read_plans"])
@pytest.mark.parametrize("kind", ["inspect", "query"])
def test_mcp_canary_reads_nested_collection_operations(tmp_path, tool, kind):
    from fastmcp import FastMCP

    server = FastMCP("test")
    calls = []

    @server.tool(name=tool)
    def collection_read(operation: dict) -> dict:
        calls.append(operation)
        return {"ok": True, "hits": [{"ref": "private-reference", "text": "material"}]}

    operation = {"kind": kind, "collection": "example"}
    case = _canary_case()
    case.update(tool=tool, arguments={"operation": operation})
    report = _run_canary(tmp_path, server, [case], samples=1, warm_up=False, check="availability")
    assert report["check_verdict"] == "passed"
    assert calls == [operation]
    assert report["cases"][0]["observed_terminals"] == 1


@pytest.mark.parametrize("tool", ["read_records", "read_plans"])
@pytest.mark.parametrize(
    "arguments",
    [
        {"operation": {"kind": "import", "import_request": {"mode": "preview"}}},
        {"operation": {"kind": "append", "collection": "example", "item": {}}},
        {"kind": "inspect"},
        {"operation": {"kind": ["inspect"]}},
        {"operation": {"kind": "query"}},
        {"operation": {"kind": "inspect", "collection": 42}},
        {"operation": {"kind": "inspect", "item": {}}},
    ],
)
def test_mcp_collection_mutations_and_malformed_operations_fail_before_connection(
    tmp_path, tool, arguments
):
    class NoConnection:
        async def __aenter__(self):
            pytest.fail("invalid collection operations must not connect")

    case = _canary_case()
    case.update(tool=tool, arguments=arguments)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([case]))
    report = asyncio.run(
        gate.run_mcp_canary(client=NoConnection(), cases_file=path, package_mode="source")
    )
    assert report["failure"] == "invalid-cases"
    assert report["initialization_ms"] is None


def test_mcp_deep_recall_cannot_pass_the_ordinary_population(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str, deep: bool) -> dict:
        assert deep
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    case = _canary_case()
    case["arguments"]["deep"] = True
    report = _run_canary(tmp_path, server, [case])
    assert report["availability"] == "passed"
    assert report["verdict"] == "incomplete"
    [row] = report["cases"][0]["latency"]
    assert row["deep"] is True
    assert row["verdict"] == "unavailable"
    assert row["breach"] is False


@pytest.mark.parametrize("enveloped", [True, False])
def test_mcp_canary_accepts_material_from_published_recall_shapes(tmp_path, enveloped):
    from fastmcp import FastMCP

    from exomem.commands import op_ask_memory

    server = FastMCP("test")

    def ask_memory(query: str) -> dict:
        hits = [
            {
                "path": "notes/example.md",
                "type": "note",
                "scope": "example",
                "title": "Example",
                "updated": "2026-01-01",
                "ref": "private-reference",
                "excerpt": query,
            }
        ]
        return (
            {
                "hits": hits,
                "warming": {"components": ["pending_visibility"]},
            }
            if enveloped
            else hits
        )

    ask_memory.__annotations__["return"] = get_type_hints(op_ask_memory, include_extras=True)[
        "return"
    ]
    server.tool(ask_memory)
    hit_path = ["hits", 0] if enveloped else [0]
    report = _run_canary(
        tmp_path,
        server,
        [
            _canary_case(
                state_path=[*hit_path, "type"],
                state="note",
                references=[{"path": [*hit_path, "ref"], "value": "private-reference"}],
                material_paths=[[*hit_path, "excerpt"]],
            )
        ],
        samples=1,
        warm_up=False,
        check="availability",
    )
    assert report["availability"] == "passed"
    assert report["check_verdict"] == "passed"
    [attempt] = report["cases"][0]["attempts"]
    assert attempt["outcome"] == "ok"
    assert attempt["failure"] is None


def test_mcp_fast_typed_refusal_fails_and_stops_the_series(tmp_path, caplog):
    from fastmcp import FastMCP

    from exomem.cli_ops import envelope
    from exomem.commands import op_ask_memory

    server = FastMCP("test")

    def ask_memory(query: str) -> dict:
        return envelope(False, error={"code": "RETRIEVAL_INDEX_WARMING"})

    ask_memory.__annotations__["return"] = get_type_hints(op_ask_memory, include_extras=True)[
        "return"
    ]
    server.tool(ask_memory)
    report = _run_canary(tmp_path, server, [_canary_case()])
    assert report["verdict"] == "failed"
    assert report["availability"] == "failed"
    assert report["coverage"] == "incomplete"
    attempts = report["cases"][0]["attempts"]
    assert len(attempts) == 1
    assert attempts[0]["outcome"] == "refused"
    assert attempts[0]["elapsed_ms"] >= 0
    assert any(
        record.event == "recall_canary_failed"
        for record in caplog.records
        if hasattr(record, "event")
    )


@pytest.mark.parametrize(
    "response,reason",
    [
        ({"ok": True, "hits": [{"ref": "wrong", "text": "material"}]}, "missing-reference"),
        ({"ok": True, "hits": [{"ref": "private-reference", "text": "  "}]}, "missing-material"),
        ({"ok": True}, "missing-reference"),
        ({"success": False}, "unexpected-outcome"),
    ],
)
def test_mcp_positive_cases_require_source_truth_and_material(tmp_path, response, reason):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return response

    report = _run_canary(tmp_path, server, [_canary_case()])
    assert report["availability"] == "failed"
    assert report["cases"][0]["attempts"][0]["failure"] == reason


@pytest.mark.parametrize("enveloped", [True, False])
@pytest.mark.parametrize("target_material", ["bound evidence", "  "])
def test_mcp_schema_binds_material_to_the_target_at_any_rank(tmp_path, enveloped, target_material):
    from fastmcp import FastMCP

    server = FastMCP("test")
    hits = [
        {"ref": "source-other", "text": "unrelated material"},
        {"ref": "source-another", "text": "more unrelated material"},
        {"ref": "source-target", "text": target_material},
    ]

    def ask_memory(query: str) -> dict | list[dict]:
        return {"hits": hits} if enveloped else hits

    server.tool(ask_memory)
    # The pattern checks nonblank string shape; it does not interpret the material.
    target = {
        "type": "object",
        "required": ["ref", "text"],
        "properties": {
            "ref": {"const": "source-target"},
            "text": {"type": "string", "pattern": r"\S"},
        },
    }
    membership = {"type": "array", "contains": {"$ref": "#/$defs/target"}}
    schema = {
        "$defs": {"target": target},
        **(
            {"type": "object", "required": ["hits"], "properties": {"hits": membership}}
            if enveloped
            else membership
        ),
    }
    case = _canary_case()
    case["expected"] = {"response_schema": schema}
    report = _run_canary(tmp_path, server, [case], samples=1, warm_up=False, check="availability")
    attempt = report["cases"][0]["attempts"][0]
    assert attempt["outcome"] == "ok"
    assert attempt["elapsed_ms"] >= 0
    assert attempt["failure"] == (None if target_material.strip() else "unexpected-response")
    assert report["check_verdict"] == ("passed" if target_material.strip() else "failed")


def test_mcp_schema_can_require_the_target_at_an_exact_rank(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> list[dict]:
        return [
            {"ref": "source-other", "text": "other material"},
            {"ref": "source-target", "text": "target material"},
        ]

    case = _canary_case()
    case["expected"] = {
        "response_schema": {
            "type": "array",
            "minItems": 1,
            "prefixItems": [
                {
                    "type": "object",
                    "required": ["ref"],
                    "properties": {"ref": {"const": "source-target"}},
                }
            ],
        }
    }
    report = _run_canary(tmp_path, server, [case], samples=1, warm_up=False)
    assert report["availability"] == "failed"
    assert report["cases"][0]["attempts"][0]["failure"] == "unexpected-response"


def test_schema_mismatch_retains_a_fingerprint_without_private_fields_or_values(tmp_path):
    import hashlib

    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"private-slot": [{"private-key": "actual-private"}]}

    case = _canary_case()
    case["expected"] = {
        "response_schema": {
            "type": "object",
            "properties": {
                "private-slot": {
                    "type": "array",
                    "contains": {
                        "type": "object",
                        "required": ["private-key"],
                        "properties": {"private-key": {"const": "expected-private"}},
                    },
                },
            },
        },
    }
    report = _run_canary(tmp_path, server, [case], samples=1, warm_up=False, check="availability")
    [attempt] = report["cases"][0]["attempts"]
    assert attempt["failure"] == "unexpected-response"
    assert report["availability"] == report["check_verdict"] == "failed"
    schema_path = ["properties", "private-slot", "contains"]
    assert attempt["schema_failure"] == {
        "keyword": "contains",
        "schema_path_sha256": hashlib.sha256(
            json.dumps(schema_path, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    for value in (
        "private-slot",
        "private-key",
        "actual-private",
        "expected-private",
        "private-question",
    ):
        assert value not in json.dumps(report)


@pytest.mark.parametrize(
    "expected",
    [
        {"response_schema": {"type": "invalid-type"}},
        {"response_schema": {"$schema": "http://json-schema.org/draft-07/schema#"}},
        {"response_schema": {"contains": {"$schema": "http://json-schema.org/draft-07/schema#"}}},
        {"response_schema": {"$defs": {"unused": {"$schema": "https://schema.invalid/dialect"}}}},
        {**_canary_case()["expected"], "response_schema": {}},
        {"response_schema": {}, "outcome": "refused"},
        {"response_schema": False},
    ],
)
def test_mcp_invalid_schema_expectations_fail_before_connection(tmp_path, expected):
    from fastmcp import FastMCP

    case = _canary_case()
    case["expected"] = expected
    report = _run_canary(tmp_path, FastMCP("test"), [case])
    assert report["failure"] == "invalid-cases"
    assert report["initialization_ms"] is None
    assert report["cases"] == []


def test_mcp_schema_dialect_markers_in_constant_and_default_data_are_not_schemas(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")
    data = {"$schema": "http://json-schema.org/draft-07/schema#", "value": "schema-data"}

    @server.tool
    def ask_memory(query: str) -> dict:
        return data

    case = _canary_case()
    case["expected"] = {
        "response_schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "const": data,
            "default": {"$schema": "https://schema.invalid/data"},
        }
    }
    report = _run_canary(tmp_path, server, [case], samples=1, warm_up=False, check="availability")
    assert report["check_verdict"] == "passed"


@pytest.mark.parametrize("scheme", ["https", "file"])
def test_mcp_unresolved_schema_refs_are_observer_errors_without_retrieval_or_leaks(
    tmp_path, monkeypatch, capsys, scheme
):
    import urllib.request

    import fastmcp

    server = fastmcp.FastMCP("test")
    calls = []
    retrievals = []

    @server.tool
    def ask_memory(query: str) -> dict:
        calls.append(query)
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    def unexpected_retrieval(*args, **kwargs):
        retrievals.append(True)
        raise AssertionError("schema-retrieval-detail")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected_retrieval)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", unexpected_retrieval)
    external = tmp_path / "external-schema-detail.json"
    external.write_text("{}")
    uri = (
        "https://schema.invalid/schema-retrieval-detail" if scheme == "https" else external.as_uri()
    )
    broken = _canary_case()
    broken.update(id="case-02", expected={"response_schema": {"$ref": uri}})
    later = _canary_case()
    later["id"] = "case-03"
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([_canary_case(), broken, later]))
    real_client = fastmcp.Client
    monkeypatch.setattr(fastmcp, "Client", lambda transport, **kwargs: real_client(server))
    monkeypatch.setenv("TEST_CANARY_TOKEN", "observer-test-token")
    status = gate.main(
        [
            "--mcp-url",
            "https://observer.invalid/mcp",
            "--cases",
            str(path),
            "--token-env",
            "TEST_CANARY_TOKEN",
            "--package-mode",
            "source",
            "--check",
            "availability",
        ]
    )
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert status == 1
    assert report["failure"] == "invalid-expectation"
    assert report["availability"] == "incomplete"
    assert report["coverage"] == "incomplete"
    assert report["verdict"] == report["check_verdict"] == "failed"
    first, second, third = report["cases"]
    assert first["availability"] == "passed"
    assert second["availability"] == "incomplete"
    assert second["measured_attempts"] == 1
    [attempt] = second["attempts"]
    assert attempt["failure"] == "invalid-expectation"
    assert attempt["outcome"] == "ok"
    assert attempt["elapsed_ms"] >= 0
    assert third["availability"] == "unattempted"
    assert third["attempts"] == []
    assert len(calls) == 2
    assert retrievals == []
    assert json.loads(output.err)["fields"]["failure"] == "invalid-expectation"
    for detail in (uri, "schema-retrieval-detail", "private-reference", "private-question"):
        assert detail not in output.out + output.err


def test_mcp_schema_preserves_abstention_and_checks_outcome_before_the_schema(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def activate_context(query: str) -> dict:
        return {"abstained": True, "abstention": {"reason": "unresolved"}}

    case = _canary_case()
    case["tool"] = "activate_context"
    case["expected"] = {
        "outcome": "abstained",
        "response_schema": {
            "required": ["abstention"],
            "properties": {"abstention": {"const": {"reason": "unresolved"}}},
        },
    }
    report = _run_canary(tmp_path, server, [case], samples=1, warm_up=False)
    assert report["availability"] == "incomplete"
    assert report["cases"][0]["attempts"][0]["failure"] is None
    case["expected"] = {"response_schema": {"$ref": "https://schema.invalid/unresolved"}}
    report = _run_canary(tmp_path, server, [case], samples=1, warm_up=False)
    attempt = report["cases"][0]["attempts"][0]
    assert attempt["outcome"] == "abstained"
    assert attempt["failure"] == "unexpected-outcome"
    assert report["availability"] == "failed"


def test_mcp_declared_abstention_must_match_its_state(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def activate_context(query: str) -> dict:
        return {"abstained": True, "abstention": {"reason": "unresolved"}}

    case = _canary_case(
        state_path=["abstention", "reason"],
        state="unresolved",
        outcome="abstained",
        references=[],
        material_paths=[],
    )
    case["tool"] = "activate_context"
    report = _run_canary(tmp_path, server, [case])
    assert report["availability"] == "incomplete"
    assert report["cases"][0]["attempts"][0]["outcome"] == "abstained"
    assert report["verdict"] == "incomplete"
    assert report["positive_coverage"]["activate_context"]["successful_cases"] == 0
    case["expected"]["state"] = "ambiguous"
    assert _run_canary(tmp_path, server, [case])["availability"] == "failed"


def test_mcp_expected_abstention_needs_positive_coverage_for_its_tool(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def activate_context(query: str) -> dict:
        if query == "negative":
            return {"abstained": True, "abstention": {"reason": "unresolved"}}
        return {"abstained": False, "hits": [{"ref": "private-reference", "text": query}]}

    positive = _canary_case(state_path=["abstained"], state=False)
    positive["tool"] = "activate_context"
    negative = _canary_case(
        state_path=["abstention", "reason"],
        state="unresolved",
        outcome="abstained",
        references=[],
        material_paths=[],
    )
    negative.update(id="case-02", tool="activate_context", arguments={"query": "negative"})
    report = _run_canary(tmp_path, server, [positive, negative])
    assert report["verdict"] == "passed"
    assert report["positive_coverage"]["activate_context"]["successful_cases"] == 1
    assert report["cases"][1]["availability"] == "passed"
    assert report["cases"][1]["latency"][0]["abstentions"] == 30


@pytest.mark.parametrize("damage", ["write-tool", "empty-material", "bad-path", "duplicate"])
def test_mcp_invalid_cases_do_not_call_tools(tmp_path, damage):
    from fastmcp import FastMCP

    server = FastMCP("test")
    calls = []

    @server.tool
    def ask_memory(query: str) -> dict:
        calls.append(query)
        return {"ok": True}

    case = _canary_case()
    if damage == "write-tool":
        case["tool"] = "remember"
    elif damage == "empty-material":
        case["expected"]["material_paths"] = []
    elif damage == "bad-path":
        case["expected"]["state_path"] = [True]
    cases = [case, case] if damage == "duplicate" else [case]
    report = _run_canary(tmp_path, server, cases)
    assert report["verdict"] == "failed"
    assert report["failure"] == "invalid-cases"
    assert calls == []


@pytest.mark.parametrize(
    "missing",
    [
        {"contention": "unknown"},
        {"warm_up": False},
        {"deployment_verified": False},
        {"samples": 1},
    ],
)
def test_mcp_missing_reference_conditions_prevent_acceptance(tmp_path, missing):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    report = _run_canary(tmp_path, server, [_canary_case()], **missing)
    assert report["availability"] == "passed"
    assert report["verdict"] == "incomplete"


def test_mcp_source_observer_cannot_claim_an_installed_package(tmp_path):
    from fastmcp import FastMCP

    report = _run_canary(tmp_path, FastMCP("test"), [_canary_case()], package_mode="installed")
    assert report["failure"] == "invalid-observer-or-contract"
    assert report["initialization_ms"] is None


def test_mcp_cli_rejects_invalid_target_without_leaking_inputs(tmp_path, capsys):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([_canary_case()]))
    status = gate.main(
        ["--mcp-url", "file:///private-target", "--cases", str(path), "--package-mode", "source"]
    )
    output = capsys.readouterr()
    assert status != 0
    assert json.loads(output.out)["failure"] == "invalid-target"
    assert "private-target" not in output.out + output.err


def test_mcp_measured_failure_retains_all_outcomes_and_unattempted_cases(tmp_path):
    from fastmcp import FastMCP

    from exomem.cli_ops import envelope

    server = FastMCP("test")
    completed = 0

    @server.tool
    def ask_memory(query: str) -> dict:
        nonlocal completed
        completed += 1
        if completed == 3:
            return envelope(False, error={"code": "RETRIEVAL_INDEX_WARMING"})
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    later = _canary_case()
    later["id"] = "case-02"
    report = _run_canary(tmp_path, server, [_canary_case(), later])
    first, second = report["cases"]
    assert report["verdict"] == "failed"
    assert first["measured_attempts"] == 2
    assert first["latency"][0]["observed"] == 2
    assert first["latency"][0]["samples"] == 2
    assert first["latency"][0]["refusals"] == 1
    measured = sorted(
        attempt["elapsed_ms"] for attempt in first["attempts"] if attempt["phase"] == "measured"
    )
    assert first["observed_terminals"] == 2
    assert first["p50_ms"] == round(measured[0], 3)
    assert first["p95_ms"] == round(measured[1], 3)
    assert second["availability"] == "unattempted"
    assert second["attempts"] == []
    assert second["observed_terminals"] == 0
    assert second["p50_ms"] is second["p95_ms"] is None


@pytest.mark.parametrize("terminal", ["malformed", "tool-error", "timeout"])
def test_mcp_terminal_failures_have_elapsed_time_without_response_content(tmp_path, terminal):
    from fastmcp import FastMCP
    from mcp.types import CallToolResult, TextContent

    server = FastMCP("test")

    @server.tool
    async def ask_memory(query: str):
        if terminal == "timeout":
            await asyncio.sleep(1)
        return CallToolResult(
            isError=terminal == "tool-error",
            content=[TextContent(type="text", text="private-response")],
        )

    report = _run_canary(tmp_path, server, [_canary_case()], timeout_seconds=0.01, warm_up=False)
    assert report["verdict"] == "failed"
    attempt = report["cases"][0]["attempts"][0]
    assert attempt["outcome"] == "error"
    assert attempt["elapsed_ms"] >= 0
    assert len(report["cases"][0]["attempts"]) == 1
    assert report["cases"][0]["observed_terminals"] == (0 if terminal == "timeout" else 1)
    assert report["cases"][0]["p50_ms"] is not None
    assert report["cases"][0]["p95_ms"] is not None
    assert "private-response" not in json.dumps(report)


def test_mcp_bad_json_and_missing_case_files_fail_before_connection(tmp_path):
    from fastmcp import Client, FastMCP

    path = tmp_path / "cases.json"
    for content in (None, "{not json}"):
        if content:
            path.write_text(content)
        report = asyncio.run(
            gate.run_mcp_canary(
                client=Client(FastMCP("test")),
                cases_file=path,
                package_mode="source",
            )
        )
        assert report["failure"] == "invalid-cases"
        assert report["initialization_ms"] is None


def test_mcp_controller_contract_uses_its_own_revision_and_sample_count(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    contract = load_contract().model_dump(mode="json")
    contract["revision"] = "controller-test"
    bar = next(bar for bar in contract["bars"] if bar["id"] == "programme-public-warm")
    bar["min_samples"] = 31
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    report = _run_canary(tmp_path, server, [_canary_case()], contract_file=path)
    assert report["verdict"] == "incomplete"
    assert report["contract"]["revision"] == "controller-test"
    assert report["contract"]["sha256"] == gate._digest(path.read_bytes())
    latency = report["cases"][0]["latency"][0]
    assert latency["contract_revision"] == "controller-test"
    assert latency["min_samples"] == 31


@pytest.mark.parametrize("samples", [None, 1])
def test_mcp_case_bars_keep_their_thresholds_and_minimums(tmp_path, samples):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    contract = load_contract().model_dump(mode="json")
    base = next(bar for bar in contract["bars"] if bar["id"] == "programme-public-warm")
    contract["bars"].extend(
        [
            {
                **base,
                "id": "broad-budget",
                "threshold_ms": 60000,
                "min_samples": 1,
                "operations": ["ask_memory"],
            },
            {**base, "id": "narrow-budget", "threshold_ms": 0, "min_samples": 3},
        ]
    )
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    first = {**_canary_case(), "bar_id": "broad-budget"}
    second = {**_canary_case(), "id": "case-02", "bar_id": "narrow-budget"}
    report = _run_canary(tmp_path, server, [first, second], contract_file=path, samples=samples)
    broad, narrow = report["cases"]
    assert broad["bar_id"] == "broad-budget"
    assert narrow["bar_id"] == "narrow-budget"
    assert broad["requested_samples"] == narrow["requested_samples"] == (samples or 3)
    assert broad["latency"][0]["min_samples"] == 1
    assert broad["latency"][0]["verdict"] == "observed-within-bar"
    assert narrow["latency"][0]["min_samples"] == 3
    assert narrow["latency"][0]["verdict"] == ("failed" if samples is None else "insufficient")
    assert report["coverage"] == ("complete" if samples is None else "incomplete")


@pytest.mark.parametrize("damage", ["unknown", "wrong-surface", "wrong-operation"])
def test_mcp_invalid_case_bar_cannot_fall_back_or_connect(tmp_path, damage):
    class NoConnection:
        async def __aenter__(self):
            pytest.fail("every selected bar must validate before connection")

    contract = load_contract().model_dump(mode="json")
    base = next(bar for bar in contract["bars"] if bar["id"] == "programme-public-warm")
    selected = {**base, "id": "selected-budget"}
    if damage == "wrong-surface":
        selected["surface"] = "cli"
    elif damage == "wrong-operation":
        selected["operations"] = ["read_memory"]
    if damage != "unknown":
        contract["bars"].append(selected)
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract))
    case = {**_canary_case(), "id": "case-02", "bar_id": selected["id"]}
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps([_canary_case(), case]))
    report = asyncio.run(
        gate.run_mcp_canary(
            client=NoConnection(),
            cases_file=cases_path,
            package_mode="source",
            contract_file=contract_path,
        )
    )
    assert report["failure"] == "invalid-observer-or-contract"
    assert report["initialization_ms"] is None
    assert report["cases"] == []


def test_mcp_terminal_timing_excludes_costly_observer_assertions(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace

    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    assertion_seconds = 3600
    observer_offset = 0
    check_result = gate._canary_result

    def costly_assertion(*args, **kwargs):
        nonlocal observer_offset
        observer_offset += assertion_seconds
        return check_result(*args, **kwargs)

    monkeypatch.setattr(
        gate,
        "time",
        SimpleNamespace(time=time.time, perf_counter=lambda: time.perf_counter() + observer_offset),
    )
    monkeypatch.setattr(gate, "_canary_result", costly_assertion)
    report = _run_canary(
        tmp_path, server, [_canary_case()], samples=1, warm_up=False, check="availability"
    )
    [attempt] = report["cases"][0]["attempts"]
    assert report["check_verdict"] == "passed"
    assert observer_offset == assertion_seconds
    assert 0 <= attempt["elapsed_ms"] < assertion_seconds * 1000


def test_mcp_cli_emits_only_content_free_json_and_structured_warning(tmp_path, monkeypatch, capsys):
    import fastmcp

    server = fastmcp.FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": ""}]}

    real_client = fastmcp.Client
    monkeypatch.setattr(fastmcp, "Client", lambda transport, **kwargs: real_client(server))
    monkeypatch.setenv("TEST_CANARY_TOKEN", "private-token")
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([_canary_case()]))
    status = gate.main(
        [
            "--mcp-url",
            "https://private-target.invalid/mcp",
            "--cases",
            str(path),
            "--token-env",
            "TEST_CANARY_TOKEN",
            "--package-mode",
            "source",
            "--warm-up",
        ]
    )
    output = capsys.readouterr()
    assert status == 1
    assert json.loads(output.out)["availability"] == "failed"
    assert json.loads(output.err)["event"] == "recall_canary_failed"
    for secret in ("private-token", "private-target", "private-reference", "private-question"):
        assert secret not in output.out + output.err


@pytest.mark.parametrize("cause_kind", ["http", "protocol", "timeout"])
def test_cli_preserves_typed_initialization_failure_without_private_error_data(
    tmp_path, monkeypatch, capsys, cause_kind
):
    import fastmcp
    import httpx2 as httpx
    from mcp.shared.exceptions import MCPError

    request = httpx.Request(
        "POST",
        "https://example.invalid/mcp?token=example-secret",
        headers={"Authorization": "Bearer example-secret"},
    )
    if cause_kind == "http":
        cause = httpx.HTTPStatusError(
            "example-secret",
            request=request,
            response=httpx.Response(401, text="private-response", request=request),
        )
    elif cause_kind == "protocol":
        cause = MCPError(
            code=-32601, message="example-secret", data={"target": str(request.url)}
        )
    else:
        cause = httpx.ReadTimeout("example-secret", request=request)
    wrapped = RuntimeError("private-wrapper")
    wrapped.__cause__ = ExceptionGroup("private-group", [cause])

    class FailedConnection:
        async def __aenter__(self):
            raise wrapped

        async def __aexit__(self, *args):
            raise AssertionError("The connection never entered")

        async def call_tool(self, *args, **kwargs):
            raise AssertionError("Initialization must finish before a case call")

    monkeypatch.setattr(fastmcp, "Client", lambda *args, **kwargs: FailedConnection())
    monkeypatch.setenv("TEST_CANARY_TOKEN", "example-secret")
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([_canary_case()]))

    status = gate.main(
        [
            "--mcp-url", "https://example.invalid/mcp", "--cases", str(path),
            "--token-env", "TEST_CANARY_TOKEN", "--package-mode", "source",
            "--check", "availability",
        ]
    )

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert status == 1
    assert report["failure"] == "session-failed"
    assert report["failure_phase"] == "connect"
    assert report["cases"][0]["attempts"] == []
    assert report["availability"] == "unattempted"
    assert report["failure_details"] == {
        "http_status_codes": [401] if cause_kind == "http" else [],
        "mcp_error_codes": [-32601] if cause_kind == "protocol" else [],
        "timeout_observed": cause_kind == "timeout",
        "transport_error_observed": cause_kind == "timeout",
        "chain_truncated": False,
    }
    for secret in (
        "example-secret", "example.invalid", "private-response", "private-wrapper",
        "private-group", "private-question", "private-reference",
    ):
        assert secret not in output.out + output.err


def test_connection_error_details_bound_duplicate_groups_and_cycles():
    cause = RuntimeError("private-cause")
    cause.__context__ = cause
    grouped = ExceptionGroup("private-group", [cause] * 20)
    details = gate._connection_failure_details(grouped)
    assert details["chain_truncated"] is True
    assert details["http_status_codes"] == []
    assert details["mcp_error_codes"] == []
    assert "private" not in json.dumps(details)


@pytest.mark.parametrize(
    ("http_status", "mcp_error", "truncated"),
    [(401, -32603, False), (200, -32603, False), (307, -32600, True)],
)
def test_cli_distinguishes_http_refusal_from_jsonrpc_error_before_sdk_mapping(
    tmp_path, monkeypatch, capsys, http_status, mcp_error, truncated
):
    import httpx2 as httpx

    clients = []

    def reply(request):
        assert request.headers["Authorization"] == "Bearer example-secret"
        if http_status == 307:
            return httpx.Response(307, headers={"Location": "/mcp"}, text="private-response")
        if http_status == 200 and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": json.loads(request.content)["id"],
                    "error": {"code": -32603, "message": "private-response"},
                },
            )
        return httpx.Response(http_status, text="private-response")

    class OfflineHTTPClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(reply), **kwargs)
            clients.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", OfflineHTTPClient)
    monkeypatch.setenv("TEST_CANARY_TOKEN", "example-secret")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([_canary_case()]))
    status = gate.main(
        [
            "--mcp-url", "https://example.invalid/mcp", "--cases", str(cases),
            "--token-env", "TEST_CANARY_TOKEN", "--package-mode", "source",
            "--check", "availability",
        ]
    )

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert status == 1
    assert report["failure_phase"] == "connect"
    assert report["failure_details"]["mcp_error_codes"] == [mcp_error]
    assert report["failure_details"]["http_status_codes"] == []
    assert report["cases"][0]["attempts"] == []
    observed = report["http_transport_observations"]
    assert observed["client_created"] is True
    assert observed["truncated"] is truncated
    assert observed["capture_incomplete"] is False
    if truncated:
        assert len(observed["responses"]) == 16
    assert any(
        response["method"] == "POST" and response["status_code"] == http_status
        for response in observed["responses"]
    )
    assert all(
        report["started_at"] <= response["observed_at"] <= report["ended_at"]
        for response in observed["responses"]
    )
    assert clients and all(client.is_closed for client in clients)
    assert all(client.follow_redirects is False for client in clients)
    for secret in ("example-secret", "example.invalid", "private-response", "private-question"):
        assert secret not in output.out + output.err


def test_one_successful_sample_proves_availability_without_accepting_latency(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    report = _run_canary(
        tmp_path,
        server,
        [_canary_case()],
        samples=1,
        warm_up=False,
        contention="unknown",
        deployment_verified=False,
        check="availability",
    )
    assert report["verdict"] == "incomplete"
    assert report["check"] == "availability"
    assert report["check_verdict"] == "passed"
    assert report["cases"][0]["latency"][0]["samples"] == 1


def test_availability_keeps_a_latency_breach_visible_without_failing_the_check(tmp_path):
    from fastmcp import FastMCP

    server = FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    contract = load_contract().model_dump(mode="json")
    next(bar for bar in contract["bars"] if bar["id"] == "programme-public-warm")[
        "threshold_ms"
    ] = 0
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    report = _run_canary(
        tmp_path, server, [_canary_case()], contract_file=path, check="availability"
    )
    assert report["verdict"] == "failed"
    assert report["check_verdict"] == "passed"
    assert report["cases"][0]["latency"][0]["breach"] is True


def test_cli_atomically_keeps_an_owner_only_latest_report(tmp_path, monkeypatch, capsys):
    import fastmcp

    server = fastmcp.FastMCP("test")

    @server.tool
    def ask_memory(query: str) -> dict:
        return {"ok": True, "hits": [{"ref": "private-reference", "text": query}]}

    real_client = fastmcp.Client
    monkeypatch.setattr(fastmcp, "Client", lambda transport, **kwargs: real_client(server))
    monkeypatch.setenv("TEST_CANARY_TOKEN", "private-token")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([_canary_case()]))
    path = tmp_path / "latest.json"
    status = gate.main(
        [
            "--mcp-url",
            "https://private-target.invalid/mcp",
            "--cases",
            str(cases),
            "--token-env",
            "TEST_CANARY_TOKEN",
            "--package-mode",
            "source",
            "--check",
            "availability",
            "--report-file",
            str(path),
        ]
    )
    output = capsys.readouterr()
    assert status == 0
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text()) == json.loads(output.out)
    assert not list(tmp_path.glob(".canary-*"))
    assert "private-token" not in path.read_text() + output.out + output.err
